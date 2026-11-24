#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WoW Forever — дашборд кулинарии (EU PvP Орда).

Локальный веб-сервер на встроенном http.server:
  * подтягивает цены из TSM Public Data (items.csv) для EU / forever / pvp-horde;
  * считает себестоимость, прибыль вендору и на аукционе (с 5% комиссией);
  * отдаёт данные в одностраничный интерфейс index.html;
  * сам обновляет цены каждые 3 часа, пока запущен.

Запуск:  python dashboard_server.py  [--port 8080] [--no-browser]
"""

import argparse
import csv
import io
import json
import os
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPES_FILE = os.path.join(BASE_DIR, "recipes_cache.json")
VENDOR_FILE = os.path.join(BASE_DIR, "vendor_prices.json")
PRICES_CACHE = os.path.join(BASE_DIR, "tsm_prices_cache.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

TSM_GAME_TYPE = "forever"
TSM_REGION = "eu"
TSM_REALM_CANDIDATES = ["pvp-horde", "eu-pvp-horde", "pvp"]

AH_COMMISSION = 0.05          # 5% комиссия аукциона
REFRESH_HOURS = 3             # автообновление цен TSM
PRICE_NAP_FACTOR = 1.20       # запас к цене закупки в списке Auctionator
HOST = "127.0.0.1"
DEFAULT_PORT = 8080
AUTO_PUBLISH = os.environ.get("DASHBOARD_AUTOPUBLISH", "1").lower() not in ("0", "false", "no", "off")

# Колонки TSM, которые могут встретиться в публичном CSV (разные версии/сервера).
SALE_RATE_FIELDS = ["saleRate", "regionSaleRate", "avgSaleRate"]
SOLD_PER_DAY_FIELDS = ["soldPerDay", "regionSoldPerDay", "avgDailySold"]
LOT_FIELDS = ["quantity", "numAuctions", "numAuctionsRegion"]


# ----------------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                moment = datetime.strptime(text, fmt)
                break
            except Exception:
                moment = None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def to_num(value):
    if value is None:
        return 0.0
    try:
        text = str(value).strip().replace(",", ".")
        if not text:
            return 0.0
        return float(text)
    except Exception:
        return 0.0


def first_num(entry, fields):
    for field in fields:
        if field in entry:
            value = to_num(entry[field])
            if value > 0:
                return value
    return 0.0


def clamp(value, low, high):
    return max(low, min(high, value))


def money_text(copper):
    """Медь -> '1 зол. 25 сер. 7 мед.' (для CSV и подсказок)."""
    if copper is None:
        return "—"
    copper = int(round(copper))
    if copper == 0:
        return "0 мед."
    sign = "-" if copper < 0 else ""
    copper = abs(copper)
    parts = []
    g, s, c = copper // 10000, (copper % 10000) // 100, copper % 100
    if g:
        parts.append("%d зол." % g)
    if s:
        parts.append("%d сер." % s)
    if c:
        parts.append("%d мед." % c)
    return sign + " ".join(parts)


# ----------------------------------------------------------------------------
# Данные рецептов и вендорских цен
# ----------------------------------------------------------------------------
def load_recipes():
    with open(RECIPES_FILE, "r", encoding="utf-8") as handle:
        recipes = json.load(handle)
    if not isinstance(recipes, list) or not recipes:
        raise RuntimeError("recipes_cache.json пуст или повреждён")
    return recipes


def load_vendor_prices():
    if not os.path.exists(VENDOR_FILE):
        return {}
    try:
        with open(VENDOR_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception as exc:
        print("[вендор] не удалось прочитать vendor_prices.json: %s" % exc)
        return {}

    items = raw.get("items", raw) if isinstance(raw, dict) else {}
    result = {}
    for item_id, value in items.items():
        if str(item_id).startswith("_"):
            continue
        if isinstance(value, dict):
            price = to_num(value.get("price"))
            name = value.get("name") or ""
        else:
            price = to_num(value)
            name = ""
        if price > 0:
            result[str(item_id)] = {"price": price, "name": name}
    return result


# ----------------------------------------------------------------------------
# TSM Public Data
# ----------------------------------------------------------------------------
class TsmError(Exception):
    pass


def tsm_sources():
    urls = []
    for slug in TSM_REALM_CANDIDATES:
        urls.append(("EU PvP Орда (%s)" % slug,
                     "https://public-data.tradeskillmaster.com/%s/%s/realm/%s/items.csv"
                     % (TSM_GAME_TYPE, TSM_REGION, slug)))
    urls.append(("Весь регион EU (запасной вариант)",
                 "https://public-data.tradeskillmaster.com/%s/%s/region/items.csv"
                 % (TSM_GAME_TYPE, TSM_REGION)))
    return urls


def download_tsm_prices(progress=None):
    errors = []
    for label, url in tsm_sources():
        if progress:
            progress("Скачиваю TSM: %s" % label)
        print("[TSM] %s -> %s" % (label, url))
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (WoW-Forever-Dashboard)"})
            raw = urllib.request.urlopen(request, timeout=40).read().decode("utf-8", "replace")
            reader = csv.DictReader(io.StringIO(raw))
            columns = list(reader.fieldnames or [])
            prices = {}
            updated_at = ""
            for row in reader:
                item_id = str(row.get("itemId", "")).strip()
                if not item_id:
                    continue
                entry = {}
                for key, value in row.items():
                    if key is None:
                        continue
                    text = (value or "").strip()
                    if not text:
                        continue
                    if key.lower() == "name":
                        entry["name"] = text
                        continue
                    if key.lower() == "updatedat":
                        if not updated_at:
                            updated_at = text
                        continue
                    entry[key] = to_num(text)
                prices[item_id] = entry
            if not prices:
                errors.append("%s: пустой CSV" % label)
                continue
            if progress:
                progress("Загружено предметов: %d" % len(prices))
            print("[TSM] ok: %d предметов, колонки: %s" % (len(prices), ", ".join(columns)))
            return {
                "prices": prices,
                "columns": columns,
                "updated_at": updated_at or now_iso(),
                "source": label,
                "source_url": url,
                "fetched_at": now_iso(),
                "rows": len(prices),
            }
        except Exception as exc:
            errors.append("%s: %s" % (label, exc))
            print("[TSM] не подошёл (%s)" % exc)

    raise TsmError("Не удалось загрузить TSM Public Data. " + " | ".join(errors))


def save_prices_cache(payload):
    tmp = PRICES_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, PRICES_CACHE)


def load_prices_cache():
    if not os.path.exists(PRICES_CACHE):
        return None
    try:
        with open(PRICES_CACHE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("prices"):
            payload["from_cache"] = True
            return payload
    except Exception as exc:
        print("[кэш] не читается: %s" % exc)
    return None


def cache_age_hours(payload):
    moment = parse_iso(payload.get("fetched_at") or "")
    if not moment:
        return 10 ** 6
    return (datetime.now(timezone.utc) - moment).total_seconds() / 3600.0


# ----------------------------------------------------------------------------
# Экономика и ликвидность
# ----------------------------------------------------------------------------
def liquidity_of(entry):
    """Ликвидность лота: реальные saleRate/soldPerDay, если TSM их отдаёт, иначе прокси."""
    info = {
        "has_auction": False,
        "sale_rate": None,
        "sold_per_day": None,
        "lots": None,
        "score": 0,
        "level": "none",
        "label": "нет лотов",
        "note": "",
        "spread_pct": None,
        "trend_pct": None,
        "min_buyout": 0,
        "market_value": 0,
        "recent": 0,
        "historical": 0,
    }
    if not entry:
        info["note"] = "предмета нет в базе TSM"
        return info

    min_buyout = to_num(entry.get("minBuyout"))
    market_value = to_num(entry.get("marketValue"))
    recent = to_num(entry.get("recent"))
    historical = to_num(entry.get("historical"))
    info.update({
        "min_buyout": int(round(min_buyout)),
        "market_value": int(round(market_value)),
        "recent": int(round(recent)),
        "historical": int(round(historical)),
    })
    if min_buyout <= 0 and market_value <= 0:
        info["note"] = "нет активных лотов"
        return info

    info["has_auction"] = True
    sale_rate = first_num(entry, SALE_RATE_FIELDS)
    sold_per_day = first_num(entry, SOLD_PER_DAY_FIELDS)
    lots = first_num(entry, LOT_FIELDS)
    info["sale_rate"] = round(sale_rate, 3) if sale_rate else None
    info["sold_per_day"] = round(sold_per_day, 1) if sold_per_day else None
    info["lots"] = int(lots) if lots else None

    if market_value > 0 and min_buyout > 0:
        info["spread_pct"] = round((min_buyout - market_value) / market_value * 100.0, 1)
    if historical > 0 and recent > 0:
        info["trend_pct"] = round((recent - historical) / historical * 100.0, 1)

    if sale_rate > 0 or sold_per_day > 0:
        info["note"] = "реальная статистика TSM"
        score = 0.0
        if sale_rate > 0:
            score += clamp(sale_rate * 120.0, 0, 70)
        if sold_per_day > 0:
            score += clamp(sold_per_day * 2.0, 0, 30)
        info["score"] = int(round(clamp(score, 5, 100)))
    else:
        # TSM для 'forever' отдаёт только marketValue / minBuyout / recent / historical.
        # Оцениваем активность рынка по спреду минимального лота к рыночной цене и тренду.
        score = 45.0
        spread = info["spread_pct"]
        trend = info["trend_pct"]
        if spread is not None:
            if -15 <= spread <= 10:
                score += 22          # лоты стоят ровно по рынку -> товар активно торгуется
            elif spread < -40:
                score -= 18          # один демпинговый лот, рынок тонкий
            elif spread < -15:
                score -= 6
            elif spread > 25:
                score -= 10          # минимальный лот выше рынка -> мало предложения
        if trend is not None:
            if trend >= 5:
                score += 14
            elif trend <= -5:
                score -= 12
        if min_buyout > 0:
            score += 8
        info["score"] = int(round(clamp(score, 5, 97)))
        parts = []
        if spread is not None:
            parts.append("спред %+.0f%%" % spread)
        if trend is not None:
            parts.append("тренд %+.0f%%" % trend)
        info["note"] = "оценка по спреду/тренду" + (" (" + ", ".join(parts) + ")" if parts else "")

    score = info["score"]
    if score >= 70:
        info["level"], info["label"] = "high", "Высокая"
    elif score >= 45:
        info["level"], info["label"] = "medium", "Средняя"
    else:
        info["level"], info["label"] = "low", "Низкая"
    return info


def compute_dish(recipe, prices, vendor_prices):
    craft_qty = int(recipe.get("craft_qty") or 1) or 1
    vendor_unit = int(recipe.get("vendor_copper") or 0)
    dish_id = str(recipe.get("dish_id"))

    reagents = []
    cost_total = 0
    cost_market_total = 0
    cost_complete = True
    unknown = []

    for reagent in recipe.get("reagents", []):
        rid = str(reagent.get("id"))
        qty = int(reagent.get("qty") or 1) or 1
        entry = prices.get(rid) or {}
        ah_min = int(round(to_num(entry.get("minBuyout"))))
        ah_market = int(round(to_num(entry.get("marketValue"))))
        vendor = vendor_prices.get(rid)

        candidates = []
        if vendor and vendor["price"] > 0:
            candidates.append((vendor["price"], "vendor"))
        if ah_min > 0:
            candidates.append((ah_min, "auction"))
        elif ah_market > 0:
            candidates.append((ah_market, "auction_market"))

        if candidates:
            unit_price, source = min(candidates, key=lambda pair: pair[0])
        else:
            unit_price, source = None, "unknown"
            cost_complete = False
            unknown.append(reagent.get("name") or ("item %s" % rid))

        sold_total = (unit_price or 0) * qty
        cost_total += sold_total
        cost_market_total += (ah_market if ah_market > 0 else (unit_price or 0)) * qty

        reagents.append({
            "id": rid,
            "name": reagent.get("name") or ("Item %s" % rid),
            "name_en": entry.get("name") or "",
            "qty": qty,
            "unit_price": None if unit_price is None else int(unit_price),
            "unit_price_source": source,
            "vendor_price": (vendor["price"] if vendor else None),
            "ah_min": ah_min or None,
            "ah_market": ah_market or None,
            "total": int(sold_total),
            "total_market": int((ah_market if ah_market > 0 else (unit_price or 0)) * qty),
            "liquidity": liquidity_of(entry).get("level") if entry else "none",
        })

    dish_entry = prices.get(dish_id) or {}
    dish_min = int(round(to_num(dish_entry.get("minBuyout"))))
    dish_market = int(round(to_num(dish_entry.get("marketValue"))))

    vendor_revenue = vendor_unit * craft_qty
    vendor_profit = vendor_revenue - cost_total

    ah_price_unit = dish_min or dish_market
    ah_revenue = ah_price_unit * craft_qty
    ah_profit = None
    if ah_price_unit > 0:
        ah_profit = int(round(ah_revenue * (1.0 - AH_COMMISSION))) - int(cost_total)

    ah_revenue_market = dish_market * craft_qty
    ah_profit_market = None
    if dish_market > 0:
        ah_profit_market = int(round(ah_revenue_market * (1.0 - AH_COMMISSION))) - int(cost_total)

    liquidity = liquidity_of(dish_entry)

    liq_level = liquidity.get("level") or "none"
    if not cost_complete:
        verdict, verdict_code = "Проверить", "warn"
        reason = "нет цены: " + ", ".join(unknown[:3])
    elif vendor_profit > 0:
        # Вендор — гарантированные деньги без комиссии и без риска.
        # На аукцион отправляем только если там ощутимо жирнее и рынок живой.
        if (ah_profit is not None and ah_profit > vendor_profit * 1.5 and liq_level in ("medium", "high")):
            verdict, verdict_code = "На аукцион", "ah"
            reason = "на аукционе %s после комиссии 5%% (в 1.5+ раза выгоднее вендора)" % money_text(ah_profit)
        else:
            verdict, verdict_code = "Вендору", "vendor"
            reason = "гарантированные %s с крафта без комиссии и риска" % money_text(vendor_profit)
    elif ah_profit is not None and ah_profit > 0:
        verdict, verdict_code = "На аукцион", "ah"
        reason = "вендор убыточен, на аукционе %s после комиссии" % money_text(ah_profit)
    else:
        verdict, verdict_code = "Не крафтить", "none"
        reason = "прибыли нет ни у вендора, ни на аукционе"

    return {
        "id": dish_id,
        "name": recipe.get("dish_name") or ("Item %s" % dish_id),
        "name_en": dish_entry.get("name") or "",
        "skill": int(recipe.get("skill") or 0),
        "craft_qty": craft_qty,
        "cost": int(round(cost_total)),
        "cost_market": int(round(cost_market_total)),
        "cost_complete": cost_complete,
        "unknown_reagents": unknown,
        "vendor_unit": vendor_unit,
        "vendor_revenue": vendor_revenue,
        "vendor_profit": int(round(vendor_profit)),
        "ah_unit": ah_price_unit,
        "ah_unit_market": dish_market,
        "ah_revenue": ah_revenue,
        "ah_profit": ah_profit,
        "ah_profit_market": ah_profit_market,
        "ah_commission": int(round(ah_revenue * AH_COMMISSION)) if ah_price_unit > 0 else None,
        "liquidity": liquidity,
        "verdict": verdict,
        "verdict_code": verdict_code,
        "verdict_reason": reason,
        "reagents": reagents,
        "wowhead": "https://www.wowhead.com/forever/ru/item=%s" % dish_id,
    }


# ----------------------------------------------------------------------------
# Строки импорта для Auctionator
# ----------------------------------------------------------------------------
FORMATS = {
    "semicolon": {
        "title": "Auctionator: Название;;цена;кол-во",
        "hint": "Название;;макс. цена в МЕДИ;количество — строковый формат списка покупок",
    },
    "tab": {
        "title": "Auctionator: по столбцам (Tab)",
        "hint": "Название <Tab> количество <Tab> макс. цена в меди (удобно вставлять в таблицу)",
    },
    "simple": {
        "title": "Простой список покупок (только названия)",
        "hint": "по одному названию в строке — самый совместимый вариант для Auctionator",
    },
    "money": {
        "title": "Человеческая цена (зол/сер/медь)",
        "hint": "Название;;кол-во;;цена 1 зол. 25 сер. — для ручной проверки",
    },
}


def build_shopping_list(items, fmt, list_name):
    lines = []
    if fmt == "semicolon":
        for item in items:
            lines.append("%s;;%d;%d" % (item["name"], item["max_price"], item["qty"]))
    elif fmt == "tab":
        for item in items:
            lines.append("%s\t%d\t%d" % (item["name"], item["qty"], item["max_price"]))
    elif fmt == "simple":
        for item in items:
            lines.append(item["name"])
    elif fmt == "money":
        for item in items:
            lines.append("%s;;%d;;%s" % (item["name"], item["qty"], money_text(item["max_price"])))
    else:
        raise ValueError("неизвестный формат: %s" % fmt)
    return "\n".join(lines)


def max_buy_price(reagent):
    base = reagent.get("ah_min") or reagent.get("ah_market") or reagent.get("vendor_price") or 0
    if base <= 0:
        return 0
    price = base * PRICE_NAP_FACTOR
    rounding = 100.0 if price >= 5000 else (10.0 if price >= 100 else 1.0)
    return int(round(price / rounding) * rounding) or int(round(price))


def shopping_items_from_dish(dish, multiplier, lang):
    items = []
    for reagent in dish["reagents"]:
        name = reagent["name_en"] if (lang == "en" and reagent.get("name_en")) else reagent["name"]
        items.append({
            "id": reagent["id"],
            "name": name,
            "qty": reagent["qty"] * multiplier,
            "max_price": max_buy_price(reagent),
            "unit": reagent["unit_price"],
        })
    return items


def shopping_items_all(data, multiplier, lang):
    merged = {}
    for dish in data["dishes"]:
        if not dish["cost_complete"] or dish["vendor_profit"] <= 0:
            continue
        for item in shopping_items_from_dish(dish, multiplier, lang):
            key = item["id"]
            if key not in merged:
                merged[key] = dict(item)
            else:
                merged[key]["qty"] += item["qty"]
                prices = [p for p in (merged[key]["max_price"], item["max_price"]) if p > 0]
                merged[key]["max_price"] = min(prices) if prices else 0
    return sorted(merged.values(), key=lambda entry: (-entry["max_price"] * entry["qty"], entry["name"]))


# ----------------------------------------------------------------------------
# Хранилище состояния + фоновое обновление
# ----------------------------------------------------------------------------
class Dashboard(object):
    def __init__(self):
        self.lock = threading.RLock()
        self.recipes = []
        self.vendor_prices = {}
        self.tsm = None
        self.data = None
        self.status = {
            "state": "idle",
            "message": "Готово",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "next_refresh_at": None,
        }
        self._update_lock = threading.Lock()

    def _set_status(self, **kwargs):
        with self.lock:
            self.status.update(kwargs)

    def progress(self, message):
        self._set_status(message=message)

    def load_cache(self):
        cached = load_prices_cache()
        if cached:
            with self.lock:
                self.tsm = cached
            print("[кэш] цены TSM от %s (%d предметов)%s" % (
                cached.get("updated_at") or "?", cached.get("rows") or len(cached.get("prices") or {}),
                ", из файла" if cached.get("from_cache") else ""))
        return cached

    def rebuild(self):
        with self.lock:
            vendor_prices = load_vendor_prices()
            self.vendor_prices = vendor_prices
            if not self.recipes:
                self.recipes = load_recipes()
            prices = (self.tsm or {}).get("prices") or {}

            dishes = [compute_dish(recipe, prices, vendor_prices) for recipe in self.recipes]
            dishes.sort(key=lambda dish: (dish["skill"], dish["name"]))

            profitable = [dish for dish in dishes if dish["cost_complete"] and dish["vendor_profit"] > 0]
            high_liquid = [dish for dish in dishes if dish["liquidity"]["level"] == "high"]
            best = max(profitable, key=lambda dish: dish["vendor_profit"]) if profitable else None

            meta = self.tsm or {}
            self.data = {
                "generated_at": now_iso(),
                "tsm": {
                    "updated_at": meta.get("updated_at"),
                    "fetched_at": meta.get("fetched_at"),
                    "source": meta.get("source"),
                    "source_url": meta.get("source_url"),
                    "rows": meta.get("rows") or len(prices),
                    "columns": meta.get("columns") or [],
                    "from_cache": bool(meta.get("from_cache")),
                    "has_sale_rate": any(field in (meta.get("columns") or []) for field in SALE_RATE_FIELDS + SOLD_PER_DAY_FIELDS),
                    "has_lots": any(field in (meta.get("columns") or []) for field in LOT_FIELDS),
                },
                "settings": {
                    "ah_commission_pct": int(AH_COMMISSION * 100),
                    "refresh_hours": REFRESH_HOURS,
                    "price_nap_factor": PRICE_NAP_FACTOR,
                },
                "stats": {
                    "total": len(dishes),
                    "vendor_profitable": len(profitable),
                    "high_liquid": len(high_liquid),
                    "no_cost": len([dish for dish in dishes if not dish["cost_complete"]]),
                    "best_name": best["name"] if best else None,
                    "best_profit": best["vendor_profit"] if best else 0,
                },
                "dishes": dishes,
            }
        return self.data

    def refresh(self, reason="manual"):
        if not self._update_lock.acquire(False):
            return False
        try:
            self._set_status(state="updating", message="Обновляю цены TSM…",
                             started_at=now_iso(), error=None, reason=reason)
            payload = download_tsm_prices(progress=self.progress)
            save_prices_cache(payload)
            with self.lock:
                self.tsm = payload
            self.rebuild()
            self._set_status(state="idle", message="Цены обновлены", finished_at=now_iso(),
                             next_refresh_at=self.next_refresh_iso(), error=None)
            print("[TSM] обновление завершено: %s, предметов %d" % (payload["updated_at"], payload["rows"]))
            self.publish_static()
            return True
        except Exception as exc:
            print("[TSM] ошибка обновления: %s" % exc)
            self._set_status(state="error", message="Не удалось обновить TSM", error=str(exc),
                             finished_at=now_iso(), next_refresh_at=self.next_refresh_iso())
            return False
        finally:
            self._update_lock.release()

    def next_refresh_iso(self):
        moment = datetime.now(timezone.utc).timestamp() + REFRESH_HOURS * 3600
        return datetime.fromtimestamp(moment, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def start_background(self):
        thread = threading.Thread(target=self._loop, name="tsm-refresher", daemon=True)
        thread.start()

    def publish_static(self):
        """После обновления цен пересобрать docs/index.html и (если есть remote) запушить."""
        if not AUTO_PUBLISH:
            return

        def worker(data):
            try:
                import build_static_site
                build_static_site.publish_from_data(data)
            except Exception as exc:
                print("[static] публикация не удалась: %s" % exc)

        data = self.data
        if not data:
            return
        threading.Thread(target=worker, args=(data,), name="static-publish", daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(REFRESH_HOURS * 3600)
            print("[фон] плановое обновление TSM")
            self.refresh("schedule")

    def status_snapshot(self):
        with self.lock:
            snapshot = dict(self.status)
            snapshot["data_ready"] = self.data is not None
            snapshot["dish_count"] = len(self.data["dishes"]) if self.data else 0
            snapshot["refresh_hours"] = REFRESH_HOURS
        return snapshot


DASH = Dashboard()


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "WoWForeverDashboard/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("DASHBOARD_VERBOSE"):
            sys.stdout.write("[http] " + (fmt % args) + "\n")

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path == "/api/data":
                data = DASH.data or DASH.rebuild()
                payload = dict(data)
                payload["status"] = DASH.status_snapshot()
                return self._json(payload)
            if path == "/api/status":
                return self._json(DASH.status_snapshot())
            if path == "/api/update":
                return self._handle_update()
            if path == "/api/auctionator":
                return self._handle_auctionator(query)
            if path == "/api/formats":
                return self._json(FORMATS)
            if path == "/api/export.csv":
                return self._handle_export(query)
            if path == "/api/health":
                return self._json({"ok": True, "time": now_iso()})
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._json({"error": "not found", "path": path}, 404)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/update":
            return self._handle_update()
        return self._json({"error": "not found"}, 404)

    def _serve_index(self):
        if not os.path.exists(INDEX_FILE):
            return self._send(500, "index.html не найден рядом с dashboard_server.py", "text/plain; charset=utf-8")
        with open(INDEX_FILE, "r", encoding="utf-8") as handle:
            html = handle.read()
        return self._send(200, html, "text/html; charset=utf-8")

    def _handle_update(self):
        if DASH.status.get("state") == "updating":
            return self._json({"ok": False, "started": False, "status": DASH.status_snapshot()})
        thread = threading.Thread(target=DASH.refresh, kwargs={"reason": "button"}, daemon=True)
        thread.start()
        return self._json({"ok": True, "started": True, "status": DASH.status_snapshot()})

    def _handle_auctionator(self, query):
        fmt = (query.get("fmt") or ["semicolon"])[0]
        scope = (query.get("scope") or ["dish"])[0]
        lang = (query.get("lang") or ["ru"])[0]
        try:
            multiplier = max(1, int((query.get("qty") or ["1"])[0]))
        except ValueError:
            multiplier = 1

        if fmt not in FORMATS:
            return self._json({"error": "неизвестный формат"}, 400)

        data = DASH.data or DASH.rebuild()
        if scope == "all":
            items = shopping_items_all(data, multiplier, lang)
            list_name = "Кулинария: профит вендору"
        elif scope == "reagent":
            reagent_id = (query.get("id") or [""])[0]
            found = None
            for dish in data["dishes"]:
                for reagent in dish["reagents"]:
                    if reagent["id"] == reagent_id:
                        found = reagent
                        break
                if found:
                    break
            if not found:
                return self._json({"error": "реагент не найден"}, 404)
            name = found["name_en"] if (lang == "en" and found.get("name_en")) else found["name"]
            items = [{
                "id": found["id"],
                "name": name,
                "qty": multiplier,
                "max_price": max_buy_price(found),
                "unit": found["unit_price"],
            }]
            list_name = "Кулинария: %s" % name
        else:
            dish_id = (query.get("id") or [""])[0]
            dish = next((entry for entry in data["dishes"] if entry["id"] == dish_id), None)
            if not dish:
                return self._json({"error": "блюдо не найдено"}, 404)
            items = shopping_items_from_dish(dish, multiplier, lang)
            list_name = "Кулинария: %s" % dish["name"]

        if not items:
            return self._json({"error": "нечего импортировать"}, 400)

        text = build_shopping_list(items, fmt, list_name)
        total_cost = sum(item["qty"] * item["max_price"] for item in items if item["max_price"])
        return self._json({
            "text": text,
            "items": items,
            "count": len(items),
            "list_name": list_name,
            "format": fmt,
            "format_title": FORMATS[fmt]["title"],
            "format_hint": FORMATS[fmt]["hint"],
            "multiplier": multiplier,
            "estimated_cost": int(total_cost),
            "estimated_cost_text": money_text(total_cost),
        })

    def _handle_export(self, query):
        delimiter = (query.get("delimiter") or [";"])[0][:1] or ";"
        data = DASH.data or DASH.rebuild()
        buffer = io.StringIO()
        fields = ["Блюдо", "Навык", "Порций за крафт", "Себестоимость (медь)", "Себестоимость",
                  "Цена вендору (медь)", "Цена вендору", "Прибыль вендору (медь)", "Прибыль вендору",
                  "Аукцион MinBuyout (медь)", "Прибыль аукцион (медь)", "Прибыль аукцион",
                  "Комиссия 5% (медь)", "Ликвидность", "Оценка выкупа", "Вердикт", "Реагенты"]
        writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=delimiter)
        writer.writeheader()
        for dish in data["dishes"]:
            writer.writerow({
                "Блюдо": dish["name"],
                "Навык": dish["skill"],
                "Порций за крафт": dish["craft_qty"],
                "Себестоимость (медь)": dish["cost"],
                "Себестоимость": money_text(dish["cost"]),
                "Цена вендору (медь)": dish["vendor_revenue"],
                "Цена вендору": money_text(dish["vendor_revenue"]),
                "Прибыль вендору (медь)": dish["vendor_profit"],
                "Прибыль вендору": money_text(dish["vendor_profit"]),
                "Аукцион MinBuyout (медь)": dish["ah_unit"],
                "Прибыль аукцион (медь)": dish["ah_profit"] if dish["ah_profit"] is not None else "",
                "Прибыль аукцион": money_text(dish["ah_profit"]),
                "Комиссия 5% (медь)": dish["ah_commission"] if dish["ah_commission"] is not None else "",
                "Ликвидность": dish["liquidity"]["label"],
                "Оценка выкупа": dish["liquidity"]["score"],
                "Вердикт": dish["verdict"],
                "Реагенты": ", ".join("%s x%d (%s)" % (r["name"], r["qty"], money_text(r["unit_price"]))
                                       for r in dish["reagents"]),
            })
        body = "\ufeff" + buffer.getvalue()
        headers = {"Content-Disposition": 'attachment; filename="wow_forever_cooking_dashboard.csv"'}
        return self._send(200, body, "text/csv; charset=utf-8", headers)


def build_server(port):
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Дашборд кулинарии WoW Forever (EU PvP Орда)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 62)
    print(" WoW Forever — кулинарный дашборд (EU / PvP / Орда)")
    print("=" * 62)

    cached = DASH.load_cache()
    DASH.rebuild()

    if cached is None or cache_age_hours(cached) >= REFRESH_HOURS:
        why = "кэша нет" if cached is None else "кэш старше %d ч" % REFRESH_HOURS
        print("[TSM] %s — качаю свежие цены в фоне" % why)
        threading.Thread(target=DASH.refresh, kwargs={"reason": "startup"}, daemon=True).start()
    else:
        print("[TSM] кэш свежий (%.1f ч), фоновое обновление через %d ч" % (cache_age_hours(cached), REFRESH_HOURS))

    DASH.start_background()
    DASH._set_status(next_refresh_at=DASH.next_refresh_iso())

    server = None
    port = args.port
    for candidate in range(args.port, args.port + 12):
        try:
            server = build_server(candidate)
            port = candidate
            break
        except OSError as exc:
            print("[сервер] порт %d занят (%s), пробую следующий" % (candidate, exc))

    if server is None:
        print("Не удалось занять порт. Закройте другой дашборд или укажите --port.")
        return 1

    url = "http://%s:%d" % (HOST, port)
    print("[сервер] %s" % url)
    print("[сервер] блюд в базе: %d, реагентов с ценой вендора: %d"
          % (len(DASH.data["dishes"]), len(DASH.vendor_prices)))
    print("Остановить: Ctrl+C")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[сервер] остановка…")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
