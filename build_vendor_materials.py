#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Сборка справочника безлимитных вендорских реагентов vendor_materials.json.

Источник цены: Wowhead Forever — поле buyprice в WH.Gatherer.addData(3, 16, {...})
(страницы page1-3.html) либо XML предмета на wowhead.com, если предмета нет на страницах.

В файл попадают только предметы с ценой покупки > 0. Для каждого пишется:
  price      — цена покупки у торговца в меди за 1 шт.
  unlimited  — true (эти расходники продаются у обычных вендоров без ограничения)
  source     — откуда взята цена
  note       — подсказка, правится руками

Наличие аукциона и сравнение цен — только для консольного отчёта.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE_DIR, "vendor_materials.json")
OLD_FILE = os.path.join(BASE_DIR, "vendor_prices.json")
RECIPES_FILE = os.path.join(BASE_DIR, "recipes_cache.json")
TSM_CACHE = os.path.join(BASE_DIR, "tsm_prices_cache.json")
PAGES = ["page1.html", "page2.html", "page3.html"]

# Безлимитные товары: продаются у обычных вендоров (торговцы товарами, трактирщики,
# сезонные торговцы) без ограничения запаса. Именно их можно докупать в любом количестве.
UNLIMITED = {
    "10284": "Простая мука — безлимит у торговцев товарами",
    "2678": "Пряные травы — безлимит (проверено: запас ∞)",
    "3713": "Ароматные специи — безлимит",
    "2692": "Острые специи — безлимит",
    "159": "Освежающая ключевая вода — безлимит (проверено: запас ∞)",
    "1179": "Ледяное молоко — безлимит (проверено: запас ∞)",
    "4470": "Простая древесина — безлимит у торговцев товарами",
    "4536": "Спелое красное яблоко — безлимит (проверено: запас ∞)",
    "1205": "Дынный сок — безлимит у трактирщиков",
    "2596": "Мех дворфийского эля — безлимит у трактирщиков",
    "2894": "Болтливое пиво — безлимит у трактирщиков",
    "1708": "Сладкий нектар — безлимит у трактирщиков",
    "8766": "Славная утренняя роса — безлимит у трактирщиков",
    "1645": "Сок луноягоды — безлимит у трактирщиков",
    "17194": "Праздничные пряности — сезонный вендор, безлимит в праздник",
    "17196": "Праздничная выпивка — сезонный вендор, безлимит в праздник",
}

# Предметы с ценой у вендора, но с ограниченным запасом (5–10 шт.) или не подтверждённые.
# Они остаются в файле для справки, но в расчёте себестоимости не участвуют.
LIMITED = {
    "785": "Магороза — у вендора ограниченный запас",
    "2447": "Мироцвет — у вендора ограниченный запас",
    "3357": "Жизнекорень — у вендора ограниченный запас",
    "11291": "Звездная древесина — у вендора ограниченный запас",
    "6303": "Сырая скользкая скумбрия — рыбный вендор, ограниченный запас",
    "6289": "Сырой илистый луциан — рыбный вендор, ограниченный запас",
    "6308": "Сырая зубатка ощетиненная — рыбный вендор, ограниченный запас",
    "6362": "Сырая каменношкурая треска — рыбный вендор, ограниченный запас",
    "4603": "Сырой пятнистый желтохвост — рыбный вендор, ограниченный запас",
    "8959": "Сырой гребнеплавничный палтус — рыбный вендор, ограниченный запас",
    "272941": "Толстые бревна — дорогой предмет, безлимит не подтверждён",
}

VERIFIED = {"159", "2678", "10284", "1179", "4536"}


def log(message=""):
    print(message)


def parse_item_data(text):
    items = {}
    marker = "WH.Gatherer.addData("
    pos = 0
    while True:
        pos = text.find(marker, pos)
        if pos < 0:
            break
        head = text[pos:pos + 80]
        if not re.match(r"WH\.Gatherer\.addData\(\s*3\s*,\s*16\s*,\s*", head):
            pos += len(marker)
            continue
        brace = text.find("{", pos)
        depth = 0
        in_string = False
        quote = ""
        escape = False
        end = -1
        for i in range(brace, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if in_string:
                if ch == quote:
                    in_string = False
                continue
            if ch in ('"', "'"):
                in_string = True
                quote = ch
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            break
        try:
            items.update(json.loads(text[brace:end]))
        except Exception:
            pass
        pos = end
    return items


def fetch_xml_item(item_id):
    url = "https://www.wowhead.com/forever/item=%s&xml" % item_id
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8", "replace")
    result = {"name_en": "", "buyprice": 0}
    name_match = re.search(r"<name><!\[CDATA\[(.*?)\]\]></name>", raw, re.S)
    if name_match:
        result["name_en"] = name_match.group(1).strip()
    equip_match = re.search(r"<jsonEquip><!\[CDATA\[(.*?)\]\]></jsonEquip>", raw, re.S)
    if equip_match:
        try:
            equip = json.loads("{" + equip_match.group(1) + "}")
            result["buyprice"] = int(equip.get("buyprice") or 0)
            result["sellprice"] = int(equip.get("sellprice") or 0)
        except Exception:
            pass
    return result


def money(copper):
    copper = int(copper or 0)
    gold, rest = divmod(copper, 10000)
    silver, cu = divmod(rest, 100)
    parts = []
    if gold:
        parts.append("%dз" % gold)
    if silver:
        parts.append("%dс" % silver)
    if cu or not parts:
        parts.append("%dм" % cu)
    return " ".join(parts)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    recipes = json.load(open(RECIPES_FILE, encoding="utf-8"))
    reagents = {}
    for recipe in recipes:
        for reagent in recipe.get("reagents", []):
            reagents[str(reagent["id"])] = reagent.get("name") or ""

    text = ""
    for page in PAGES:
        with open(os.path.join(BASE_DIR, page), encoding="utf-8", errors="replace") as handle:
            text += handle.read() + "\n"
    items = parse_item_data(text)
    log("[wowhead] предметов в addData: %d, уникальных реагентов: %d" % (len(items), len(reagents)))

    old = {}
    if os.path.exists(OLD_FILE):
        try:
            raw = json.load(open(OLD_FILE, encoding="utf-8"))
            old = raw.get("items", raw) if isinstance(raw, dict) else {}
        except Exception:
            old = {}

    tsm = {}
    if os.path.exists(TSM_CACHE):
        try:
            tsm = json.load(open(TSM_CACHE, encoding="utf-8")).get("prices") or {}
        except Exception:
            tsm = {}

    result = {}
    fetched = []
    for item_id, ru_name in sorted(reagents.items(), key=lambda pair: pair[1]):
        source = "wowhead-adddata"
        entry = items.get(item_id) or {}
        equip = entry.get("jsonequip") or {}
        buyprice = int(equip.get("buyprice") or 0)
        name_en = (tsm.get(item_id) or {}).get("name") or ""

        if buyprice <= 0:
            # нет на сохранённых страницах — пробуем XML Wowhead
            try:
                xml = fetch_xml_item(item_id)
                if xml.get("buyprice"):
                    buyprice = xml["buyprice"]
                    source = "wowhead-xml"
                    fetched.append(item_id)
                if not name_en:
                    name_en = xml.get("name_en") or ""
            except Exception as exc:
                log("[warn] %s (%s): XML не получен: %s" % (item_id, ru_name, exc))

        if buyprice <= 0:
            continue

        note = ""
        if item_id in old and isinstance(old[item_id], dict):
            note = old[item_id].get("note") or ""
        if item_id in UNLIMITED:
            note = UNLIMITED[item_id]
        elif item_id in LIMITED:
            note = LIMITED[item_id]
        if not note:
            note = "наличие у вендора не подтверждено — проверьте перед закупкой"

        unlimited = item_id in UNLIMITED
        result[item_id] = {
            "name": ru_name or entry.get("name_ruru") or "",
            "name_en": name_en,
            "price": buyprice,
            "unlimited": unlimited,
            "verified": item_id in VERIFIED,
            "source": source,
            "note": note,
        }

    payload = {
        "_comment": "Безлимитные вендорские реагенты WoW Forever: цена покупки у торговца в меди за 1 шт. Собрано автоматически из Wowhead (buyprice), правится руками.",
        "_comment_2": "unlimited=false исключает предмет из расчёта. Для себестоимости берётся min(цена вендора, minBuyout TSM).",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": "wowhead: WH.Gatherer.addData(3,16) + XML fallback",
        "items": result,
    }
    with open(OUT_FILE, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    log("[vendor] вендорских реагентов с ценой: %d, из них безлимитных: %d (из XML: %d)" % (
        len(result), len([item for item in result.values() if item["unlimited"]]), len(fetched)))
    log()
    log("%-6s %-32s %10s %12s %12s  %s" % ("id", "предмет", "вендор", "аукцион", "выгоднее", "примечание"))
    vendor_now = 0
    unlimited_count = 0
    for item_id, info in sorted(result.items(), key=lambda pair: pair[1]["name"]):
        if not info["unlimited"]:
            continue
        unlimited_count += 1
        ah = int(round(float((tsm.get(item_id) or {}).get("minBuyout") or 0)))
        if ah > 0:
            winner = "ВЕНДОР" if info["price"] <= ah else "аукцион"
            if info["price"] <= ah:
                vendor_now += 1
        else:
            winner = "ВЕНДОР"
            vendor_now += 1
        note = info.get("note", "")
        if note.startswith(info["name"] + " — "):
            note = note[len(info["name"]) + 3:]
        log("%-6s %-32s %10s %12s %12s  %s" % (
            item_id,
            info["name"][:32],
            money(info["price"]),
            money(ah) if ah else "—",
            winner,
            note,
        ))

    log()
    log("[vendor] сейчас выгоднее у вендора: %d из %d безлимитных" % (vendor_now, unlimited_count))
    limited = sorted((item for item in result.items() if not item[1]["unlimited"]), key=lambda pair: pair[1]["name"])
    if limited:
        log("[vendor] не считаются безлимитными (остаются в файле для справки):")
        for item_id, info in limited:
            log("  * %s %s — %s" % (item_id, info["name"], info.get("note", "")))
    missing_staples = [item_id for item_id in UNLIMITED if item_id not in result]
    log("[vendor] проверка обязательных: %s" % ("все на месте" if not missing_staples
                                                else "НЕ НАЙДЕНЫ: " + ", ".join(missing_staples)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
