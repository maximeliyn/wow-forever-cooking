import json
import re
import csv
import glob
import os
import html as html_lib
import time
import urllib.request
from io import StringIO

# ==============================================================================
# НАСТРОЙКИ СЕРВЕРА ДЛЯ TSM (WoW Forever — EU — PvP — Орда)
# ==============================================================================
TSM_GAME_TYPE = "forever"
TSM_REGION = "eu"
TSM_REALM_CANDIDATES = [
    "pvp-horde",
    "eu-pvp-horde",
    "pvp"
]
# ==============================================================================

CACHE_FILE = "recipes_cache.json"
EXISTING_CSV = "wow_forever_cooking_full.csv"
OUTPUT_CSV = "wow_forever_cooking_tsm.csv"

def format_copper(copper):
    if copper is None:
        return "0 мед."
    copper = int(round(float(copper)))
    if copper == 0:
        return "0 мед."
    sign = "-" if copper < 0 else ""
    copper = abs(copper)
    g = copper // 10000
    s = (copper % 10000) // 100
    c = copper % 100
    parts = []
    if g: parts.append(f"{g} зол.")
    if s: parts.append(f"{s} сер.")
    if c: parts.append(f"{c} мед.")
    return sign + " ".join(parts)

def extract_balanced(text, start_idx):
    open_char = text[start_idx]
    close_char = '}' if open_char == '{' else ']'
    depth = 0
    in_string = False
    quote_char = ''
    escape = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if in_string:
            if ch == quote_char:
                in_string = False
            continue
        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start_idx:i+1]
    return None

def parse_sellprice_from_html(html_text):
    if not html_text:
        return 0
    m_json = re.search(r'"sellprice"\s*:\s*(\d+)', html_text)
    if m_json:
        return int(m_json.group(1))

    sell_block = ""
    m_div = re.search(r'class=["\'][^"\']*whtt-sellprice[^"\']*["\'][^>]*>(.*?)</div>', html_text, re.S | re.I)
    if m_div:
        sell_block = m_div.group(1)
    else:
        m_txt = re.search(r'(?:Цена продажи|Sell Price)\s*:(.*?)(?:</div>|<br|</td>)', html_text, re.S | re.I)
        if m_txt:
            sell_block = m_txt.group(1)

    if not sell_block:
        return 0

    mg = re.search(r'class=["\'][^"\']*moneygold[^"\']*["\'][^>]*>\s*(\d+)', sell_block, re.I)
    ms = re.search(r'class=["\'][^"\']*moneysilver[^"\']*["\'][^>]*>\s*(\d+)', sell_block, re.I)
    mc = re.search(r'class=["\'][^"\']*moneycopper[^"\']*["\'][^>]*>\s*(\d+)', sell_block, re.I)

    gold = int(mg.group(1)) if mg else 0
    silver = int(ms.group(1)) if ms else 0
    copper = int(mc.group(1)) if mc else 0
    return gold * 10000 + silver * 100 + copper

def get_cf_session():
    try:
        from curl_cffi import requests as cf_requests
        return cf_requests.Session(impersonate="chrome")
    except ImportError:
        return None

def fetch_wowhead_tooltip(session, kind, obj_id):
    if not session:
        return {}
    urls = [
        f"https://nether.wowhead.com/forever/tooltip/{kind}/{obj_id}?locale=7",
        f"https://nether.wowhead.com/tooltip/{kind}/{obj_id}?dataEnv=1&locale=7"
    ]
    for url in urls:
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200 and resp.text.strip().startswith("{"):
                time.sleep(0.1)
                return json.loads(resp.text)
        except Exception:
            pass
    return {}

def build_or_load_recipes_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data:
                print(f"Загружен локальный кэш рецептов ({len(data)} блюд) из {CACHE_FILE}")
                return data

    print("Первичная сборка базы рецептов с ID ингредиентов (один раз ~20 сек, дальше будет мгновенно)...")
    existing_map = {}
    if os.path.exists(EXISTING_CSV):
        with open(EXISTING_CSV, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=";")
            for r in reader:
                did = str(r.get("ID блюда", "")).strip()
                if did:
                    existing_map[did] = r

    html_files = sorted(glob.glob("*.html") + glob.glob("*.htm"))
    if not html_files:
        raise FileNotFoundError("Не найдены .html файлы в папке C:\\parser для первичной сборки ID ингредиентов!")

    local_items = {}
    all_tr_blocks = []
    for target_file in html_files:
        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
            raw_html = f.read()
        for m in re.finditer(r"WH\.Gatherer\.addData\(\s*3\s*,\s*\d+\s*,\s*\{", raw_html):
            brace_pos = raw_html.find("{", m.start() + 15)
            raw_json = extract_balanced(raw_html, brace_pos)
            if raw_json:
                try:
                    local_items.update(json.loads(raw_json))
                except Exception:
                    pass
        all_tr_blocks.extend(re.findall(r'<tr[^>]*class="[^"]*listview-row[^"]*"[^>]*>(.*?)</tr>', raw_html, re.S))

    session = get_cf_session()
    recipes = []
    seen_dishes = set()
    item_name_cache = {}

    for tr in all_tr_blocks:
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) < 2:
            continue

        dish_m = re.search(r'href="[^"]*?/item=(\d+)[^"]*"', tds[0] + " " + tds[1])
        if not dish_m:
            continue

        dish_id = dish_m.group(1)
        if dish_id in seen_dishes:
            continue
        seen_dishes.add(dish_id)

        spell_m = re.search(r'href="[^"]*?/spell=(\d+)[^"]*"', tr)
        spell_id = spell_m.group(1) if spell_m else ""

        craft_qty = 1
        qty_m = re.search(r'<div[^>]*>(\d+)</div>', tds[0], re.S)
        if qty_m:
            craft_qty = int(qty_m.group(1))

        prev = existing_map.get(dish_id, {})
        dish_name = prev.get("Название блюда", "").strip()
        skill_val = int(prev.get("Навык") or 1)
        vendor_copper = int(prev.get("Цена за 1 шт. (медь)") or 0)

        reagents = []
        seen_regs = {dish_id}
        rest_html = " ".join(tds[2:])
        for rm in re.finditer(r'href="[^"]*?/item=(\d+)[^"]*"[^>]*>(.*?)</a>([^<]*)', rest_html, re.S):
            rid = rm.group(1)
            if rid in seen_regs:
                continue
            seen_regs.add(rid)
            rname = html_lib.unescape(re.sub(r"<[^>]+>", "", rm.group(2))).strip()
            if not rname:
                rname = local_items.get(rid, {}).get("name_ruru", "")
            if not rname and rid in item_name_cache:
                rname = item_name_cache[rid]
            if not rname:
                tip = fetch_wowhead_tooltip(session, "item", rid)
                rname = html_lib.unescape(str(tip.get("name", ""))).strip() or f"Item #{rid}"
                item_name_cache[rid] = rname
            qm = re.search(r"(\d+)", rm.group(3))
            rqty = int(qm.group(1)) if qm else 1
            reagents.append({"id": rid, "name": rname, "qty": rqty})

        if not reagents and spell_id:
            sp_tip = fetch_wowhead_tooltip(session, "spell", spell_id)
            sp_html = sp_tip.get("tooltip", "")
            reg_block_m = re.search(r'(?:Реагенты|Reagents)\s*:?(.*?)(?:</div>|</td>|<br\s*/?>\s*<br)', sp_html, re.S | re.I)
            if reg_block_m:
                for rm in re.finditer(r'href="[^"]*?/item=(\d+)[^"]*"[^>]*>(.*?)</a>(?:\s*\((\d+)\))?', reg_block_m.group(1), re.S):
                    rid = rm.group(1)
                    rname = html_lib.unescape(re.sub(r"<[^>]+>", "", rm.group(2))).strip()
                    if not rname:
                        rname = local_items.get(rid, {}).get("name_ruru", "")
                    if not rname and rid in item_name_cache:
                        rname = item_name_cache[rid]
                    if not rname:
                        it_tip = fetch_wowhead_tooltip(session, "item", rid)
                        rname = html_lib.unescape(str(it_tip.get("name", ""))).strip() or f"Item #{rid}"
                        item_name_cache[rid] = rname
                    rqty = int(rm.group(3)) if rm.group(3) else 1
                    reagents.append({"id": rid, "name": rname, "qty": rqty})

            if skill_val <= 1:
                sk_m = re.search(r'(?:Кулинария|Cooking)\s*\((\d+)\)', sp_html, re.I)
                if sk_m:
                    skill_val = int(sk_m.group(1))

        if not dish_name or vendor_copper == 0:
            it_tip = fetch_wowhead_tooltip(session, "item", dish_id)
            if not dish_name:
                dish_name = html_lib.unescape(str(it_tip.get("name", ""))).strip() or f"Item #{dish_id}"
            if vendor_copper == 0:
                vendor_copper = parse_sellprice_from_html(it_tip.get("tooltip", ""))

        recipes.append({
            "dish_id": dish_id,
            "dish_name": dish_name,
            "skill": skill_val,
            "craft_qty": craft_qty,
            "vendor_copper": vendor_copper,
            "reagents": reagents
        })
        print(f"[{len(recipes)}] {dish_name} -> ингредиентов: {len(reagents)}")

    recipes.sort(key=lambda x: (x["skill"], x["dish_name"]))
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(recipes, f, ensure_ascii=False, indent=2)
    print(f"Кэш рецептов сохранён в {CACHE_FILE} ({len(recipes)} шт.)\n")
    return recipes

def download_tsm_prices():
    urls_to_try = []
    for slug in TSM_REALM_CANDIDATES:
        urls_to_try.append((f"EU PvP Орда ({slug})", f"https://public-data.tradeskillmaster.com/{TSM_GAME_TYPE}/{TSM_REGION}/realm/{slug}/items.csv"))
    urls_to_try.append(("Весь регион EU (запасной)", f"https://public-data.tradeskillmaster.com/{TSM_GAME_TYPE}/{TSM_REGION}/region/items.csv"))

    prices = {}
    updated_at = ""

    for label, url in urls_to_try:
        print(f"Проверка источника TSM [{label}]: {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw_csv = urllib.request.urlopen(req, timeout=25).read().decode("utf-8")
            reader = csv.DictReader(StringIO(raw_csv))
            count = 0
            for row in reader:
                iid = str(row.get("itemId", "")).strip()
                if not iid:
                    continue
                if not updated_at and row.get("updatedAt"):
                    updated_at = row["updatedAt"]
                prices[iid] = {
                    "marketValue": int(float(row.get("marketValue") or 0)),
                    "minBuyout": int(float(row.get("minBuyout") or 0)),
                    "recent": int(float(row.get("recent") or 0)),
                    "historical": int(float(row.get("historical") or 0)),
                }
                count += 1
            if count > 0:
                print(f"  -> Успех! Загружено лотов с аукциона: {count} (обновлено TSM: {updated_at})")
                return prices, updated_at, url
        except Exception as e:
            print(f"  -> Не подошёл ({e}), пробуем следующий вариант...")

    raise RuntimeError("Не удалось скачать данные с public-data.tradeskillmaster.com!")

def main():
    recipes = build_or_load_recipes_cache()
    tsm_prices, updated_at, used_url = download_tsm_prices()

    out_rows = []
    for rec in recipes:
        did = rec["dish_id"]
        qty = rec["craft_qty"]
        vendor_total = rec["vendor_copper"] * qty

        dish_tsm = tsm_prices.get(did, {})
        dish_min_unit = dish_tsm.get("minBuyout", 0)
        dish_mkt_unit = dish_tsm.get("marketValue", 0)

        dish_min_craft = dish_min_unit * qty
        dish_mkt_craft = dish_mkt_unit * qty

        mat_min_total = 0
        mat_mkt_total = 0
        reagents_text = []
        missing_mats = []

        for reg in rec["reagents"]:
            rid = reg["id"]
            rqty = reg["qty"]
            rname = reg["name"]
            r_tsm = tsm_prices.get(rid, {})
            r_min = r_tsm.get("minBuyout", 0)
            r_mkt = r_tsm.get("marketValue", 0)

            eff_min = r_min if r_min > 0 else r_mkt
            if eff_min == 0:
                missing_mats.append(rname)

            mat_min_total += eff_min * rqty
            mat_mkt_total += r_mkt * rqty
            reagents_text.append(f"{rname} x{rqty} ({format_copper(eff_min)}/шт)")

        profit_ah_min = (dish_min_craft - mat_min_total) if (dish_min_craft > 0 and mat_min_total > 0) else 0
        profit_vendor = (vendor_total - mat_min_total) if mat_min_total > 0 else 0

        out_rows.append({
            "ID блюда": did,
            "Название блюда": rec["dish_name"],
            "Навык": rec["skill"],
            "Порций за крафт": qty,
            "Ингредиенты и цены TSM": ", ".join(reagents_text) if reagents_text else "Нет данных",
            "Себестоимость крафта MinBuyout (медь)": mat_min_total,
            "Себестоимость крафта MinBuyout": format_copper(mat_min_total),
            "Себестоимость крафта MarketValue (медь)": mat_mkt_total,
            "Себестоимость крафта MarketValue": format_copper(mat_mkt_total),
            "Цена блюда TSM MinBuyout за крафт (медь)": dish_min_craft,
            "Цена блюда TSM MinBuyout за крафт": format_copper(dish_min_craft),
            "Цена блюда TSM MarketValue за крафт (медь)": dish_mkt_craft,
            "Цена блюда TSM MarketValue за крафт": format_copper(dish_mkt_craft),
            "Цена вендору за крафт (медь)": vendor_total,
            "Цена вендору за крафт": format_copper(vendor_total),
            "Прибыль Аукцион MinBuyout (медь)": profit_ah_min,
            "Прибыль Аукцион MinBuyout": format_copper(profit_ah_min),
            "Прибыль Вендор (медь)": profit_vendor,
            "Прибыль Вендор": format_copper(profit_vendor),
            "Нет на аукционе": ", ".join(missing_mats),
            "Обновлено TSM (UTC)": updated_at
        })

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()), delimiter=";")
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"\nГотово! Актуальная таблица с ценами TSM (EU PvP Орда) сохранена в {OUTPUT_CSV} ({len(out_rows)} блюд).")

if __name__ == "__main__":
    main()
