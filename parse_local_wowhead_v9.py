import json
import re
import csv
import glob
import html as html_lib
import time
from curl_cffi import requests

session = requests.Session(impersonate="chrome")

def format_copper(copper):
    if not copper or int(copper) <= 0:
        return "0 мед."
    copper = int(copper)
    g = copper // 10000
    s = (copper % 10000) // 100
    c = copper % 100
    parts = []
    if g: parts.append(f"{g} зол.")
    if s: parts.append(f"{s} сер.")
    if c: parts.append(f"{c} мед.")
    return " ".join(parts)

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

    gold = 0
    silver = 0
    copper = 0

    mg = re.search(r'class=["\'][^"\']*moneygold[^"\']*["\'][^>]*>\s*(\d+)', sell_block, re.I)
    ms = re.search(r'class=["\'][^"\']*moneysilver[^"\']*["\'][^>]*>\s*(\d+)', sell_block, re.I)
    mc = re.search(r'class=["\'][^"\']*moneycopper[^"\']*["\'][^>]*>\s*(\d+)', sell_block, re.I)

    if mg: gold = int(mg.group(1))
    if ms: silver = int(ms.group(1))
    if mc: copper = int(mc.group(1))

    total = gold * 10000 + silver * 100 + copper
    if total > 0:
        return total

    nums = re.findall(r'(\d+)', re.sub(r'<[^>]+>', ' ', sell_block))
    if len(nums) == 1:
        return int(nums[0])
    return 0

def get_item_details(item_id, cache, local_items):
    if item_id in cache:
        return cache[item_id]

    local_info = local_items.get(item_id, {})
    local_name = local_info.get("name_ruru") or ""

    urls = [
        f"https://nether.wowhead.com/forever/tooltip/item/{item_id}?locale=7",
        f"https://nether.wowhead.com/tooltip/item/{item_id}?dataEnv=1&locale=7",
        f"https://www.wowhead.com/forever/ru/item={item_id}"
    ]

    sell_copper = int(local_info.get("sellprice") or 0)
    item_name = local_name

    if not item_name or sell_copper == 0:
        for url in urls:
            try:
                resp = session.get(url, timeout=15)
                if resp.status_code == 200:
                    text = resp.text
                    if text.strip().startswith("{"):
                        try:
                            data = json.loads(text)
                            if not item_name and data.get("name"):
                                item_name = html_lib.unescape(str(data["name"])).strip()
                            tooltip_html = data.get("tooltip", "")
                            if not item_name and tooltip_html:
                                nm = re.search(r'<b[^>]*class="[^"]*q\d*[^"]*"[^>]*>(.*?)</b>', tooltip_html, re.S)
                                if nm:
                                    item_name = html_lib.unescape(re.sub(r"<[^>]+>", "", nm.group(1))).strip()
                            if sell_copper == 0:
                                sell_copper = parse_sellprice_from_html(tooltip_html)
                        except Exception:
                            if sell_copper == 0:
                                sell_copper = parse_sellprice_from_html(text)
                    else:
                        if sell_copper == 0:
                            sell_copper = parse_sellprice_from_html(text)
                        if not item_name:
                            tm = re.search(r'<h1[^>]*class="[^"]*heading-size-1[^"]*"[^>]*>(.*?)</h1>', text, re.S)
                            if tm:
                                item_name = html_lib.unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()

                    if sell_copper > 0 and item_name:
                        break
            except Exception:
                pass
        time.sleep(0.12)

    cache[item_id] = (item_name, sell_copper)
    return cache[item_id]

def get_spell_reagents_and_skill(spell_id, spell_cache, item_cache, local_items):
    if spell_id in spell_cache:
        return spell_cache[spell_id]

    urls = [
        f"https://nether.wowhead.com/forever/tooltip/spell/{spell_id}?locale=7",
        f"https://nether.wowhead.com/tooltip/spell/{spell_id}?dataEnv=1&locale=7",
        f"https://www.wowhead.com/forever/ru/spell={spell_id}"
    ]

    reagents_list = []
    skill_val = 0

    for url in urls:
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 200:
                text = resp.text
                tooltip_html = text
                if text.strip().startswith("{"):
                    try:
                        data = json.loads(text)
                        tooltip_html = data.get("tooltip", "")
                    except Exception:
                        pass

                # 1. Ищем блок "Реагенты:" или "Reagents:" в тултипе заклинания
                reg_block_m = re.search(r'(?:Реагенты|Reagents)\s*:?(.*?)(?:</div>|</td>|<br\s*/?>\s*<br)', tooltip_html, re.S | re.I)
                if reg_block_m:
                    reg_block = reg_block_m.group(1)
                    # Вытаскиваем все ссылки на предметы в блоке реагентов и количество в скобках (2)
                    for rm in re.finditer(r'href="[^"]*?/item=(\d+)[^"]*"[^>]*>(.*?)</a>(?:\s*\((\d+)\))?', reg_block, re.S):
                        rid = rm.group(1)
                        rname = html_lib.unescape(re.sub(r"<[^>]+>", "", rm.group(2))).strip()
                        if not rname:
                            rname, _ = get_item_details(rid, item_cache, local_items)
                        rqty = rm.group(3) if rm.group(3) else "1"
                        if rname:
                            reagents_list.append(f"{rname} x{rqty}")

                # 2. Ищем требуемый уровень навыка в тултипе ("Требуется Кулинария (175)" / "Requires Cooking (175)")
                sk_m = re.search(r'(?:Кулинария|Cooking)\s*\((\d+)\)', tooltip_html, re.I)
                if sk_m:
                    skill_val = int(sk_m.group(1))

                if reagents_list:
                    break
        except Exception:
            pass

    time.sleep(0.12)
    spell_cache[spell_id] = (reagents_list, skill_val)
    return spell_cache[spell_id]

def parse_all_local_htmls():
    html_files = sorted(glob.glob("*.html") + glob.glob("*.htm"))
    if not html_files:
        raise FileNotFoundError("В папке C:\\parser не найдено ни одного .html файла!")

    local_items = {}
    all_tr_blocks = []

    for target_file in html_files:
        print(f"Чтение файла: {target_file}")
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

        tr_blocks = re.findall(r'<tr[^>]*class="[^"]*listview-row[^"]*"[^>]*>(.*?)</tr>', raw_html, re.S)
        print(f"  -> Найдено строк в файле: {len(tr_blocks)}")
        all_tr_blocks.extend(tr_blocks)

    item_cache = {}
    spell_cache = {}
    rows = []
    seen_dishes = set()

    for idx, tr in enumerate(all_tr_blocks, 1):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) < 2:
            continue

        icon_td = tds[0]
        name_td = tds[1]

        dish_m = re.search(r'href="[^"]*?/item=(\d+)[^"]*"', icon_td + " " + name_td)
        if not dish_m:
            continue

        dish_id = dish_m.group(1)
        if dish_id in seen_dishes:
            continue
        seen_dishes.add(dish_id)

        spell_m = re.search(r'href="[^"]*?/spell=(\d+)[^"]*"', tr)
        spell_id = spell_m.group(1) if spell_m else ""

        # Количество создаваемых штук (в первой ячейке в div внутри иконки)
        craft_qty = 1
        qty_m = re.search(r'<div[^>]*>(\d+)</div>', icon_td, re.S)
        if qty_m:
            craft_qty = int(qty_m.group(1))

        # Ищем ингредиенты по всем ячейкам строки (после 2-й), исключая само создаваемое блюдо
        reagents_list = []
        seen_regs = {dish_id}
        rest_html = " ".join(tds[2:])
        for reg_match in re.finditer(r'href="[^"]*?/item=(\d+)[^"]*"[^>]*>(.*?)</a>([^<]*)', rest_html, re.S):
            rid = reg_match.group(1)
            if rid in seen_regs:
                continue
            seen_regs.add(rid)
            rname = html_lib.unescape(re.sub(r"<[^>]+>", "", reg_match.group(2))).strip()
            if not rname:
                rname, _ = get_item_details(rid, item_cache, local_items)
            if not rname:
                rname = f"Item #{rid}"
            tail = reg_match.group(3)
            qty_m = re.search(r"(\d+)", tail)
            rqty = qty_m.group(1) if qty_m else "1"
            reagents_list.append(f"{rname} x{rqty}")

        # Уровень навыка ищем во всех ячейках строки (первое число в колонке навыка)
        skill_val = 0
        for td in reversed(tds):
            if 'skill' in td.lower() or 'listview-col-skill' in td.lower():
                nums = re.findall(r'>\s*(\d+)\s*<', td)
                if nums:
                    skill_val = int(nums[0])
                    break
        if skill_val == 0:
            # Часто навык лежит просто в последней ячейке без класса skill
            last_nums = re.findall(r'>\s*(\d+)\s*<', tds[-1])
            if last_nums:
                skill_val = int(last_nums[0])

        # Если в строке HTML не было ингредиентов или навыка — добираем напрямую из тултипа заклинания!
        if spell_id and (not reagents_list or skill_val <= 1):
            sp_reagents, sp_skill = get_spell_reagents_and_skill(spell_id, spell_cache, item_cache, local_items)
            if not reagents_list and sp_reagents:
                reagents_list = sp_reagents
            if sp_skill > 0:
                skill_val = sp_skill

        if skill_val <= 0:
            skill_val = 1

        api_name, sell_copper = get_item_details(dish_id, item_cache, local_items)
        dish_name = api_name or f"Item #{dish_id}"
        total_copper = sell_copper * craft_qty
        reagents_str = ", ".join(reagents_list) if reagents_list else "Нет данных"

        print(f"[{len(rows)+1}] {dish_name} (навык {skill_val}) | {reagents_str} -> {format_copper(sell_copper)}")

        rows.append({
            "ID блюда": dish_id,
            "Название блюда": dish_name,
            "Ингредиенты": reagents_str,
            "Навык": skill_val,
            "Кол-во за крафт": craft_qty,
            "Цена за 1 шт. (медь)": sell_copper,
            "Цена за 1 шт. (формат)": format_copper(sell_copper),
            "Цена за крафт (медь)": total_copper,
            "Цена за крафт (формат)": format_copper(total_copper),
            "Ссылка": f"https://www.wowhead.com/forever/ru/item={dish_id}"
        })

    rows.sort(key=lambda x: (int(x["Навык"] or 0), x["Название блюда"]))

    out_file = "wow_forever_cooking_full.csv"
    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        fieldnames = [
            "ID блюда", "Название блюда", "Ингредиенты", "Навык", "Кол-во за крафт",
            "Цена за 1 шт. (медь)", "Цена за 1 шт. (формат)",
            "Цена за крафт (медь)", "Цена за крафт (формат)", "Ссылка"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nГотово! Уникальных блюд сохранено в {out_file}: {len(rows)}")

if __name__ == "__main__":
    parse_all_local_htmls()
