#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Пересборка recipes_cache.json из сохранённых страниц Wowhead (page1-3.html).

Берём таблицу рецептов кулинарии:
  * предмет-результат (иконка) -> dish_id
  * <span class="wh-icon-text">N</span> в иконке -> сколько порций создаёт крафт
  * ячейка «Ингредиенты» -> id реагентов и их количество (нет числа = 1)
  * «Кулинария (X)» -> требуемый навык

Ничего не выдумываем: только то, что написано в HTML.
"""

import glob
import json
import os
import re
import sys
from collections import OrderedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "recipes_cache.json")
PAGES = ["page1.html", "page2.html", "page3.html"]


def log(message):
    print(message)


def read_pages():
    text = ""
    for name in PAGES:
        path = os.path.join(BASE_DIR, name)
        if not os.path.exists(path):
            raise SystemExit("нет файла %s" % path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text += handle.read() + "\n"
    return text


def parse_item_data(text):
    """WH.Gatherer.addData(3, 16, {...}) -> {itemId: data} (имя, sellprice, buyprice)."""
    items = {}
    marker = "WH.Gatherer.addData("
    pos = 0
    while True:
        pos = text.find(marker, pos)
        if pos < 0:
            break
        head = text[pos:pos + 80]
        match = re.match(r"WH\.Gatherer\.addData\(\s*3\s*,\s*16\s*,\s*", head)
        if not match:
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
        raw = text[brace:end]
        try:
            items.update(json.loads(raw))
        except Exception:
            pass
        pos = end
    return items


def strip_tags(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def parse_rows(text):
    """Все строки таблицы рецептов -> список словарей."""
    rows = []
    for match in re.finditer(r'<tr[^>]*class="[^"]*listview-row[^"]*"[^>]*>(.*?)</tr>', text, re.S):
        row = match.group(1)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 7:
            continue

        result_cell, name_cell, reagent_cell, skill_cell = tds[1], tds[2], tds[3], tds[5]

        dish_match = re.search(r"/item=(\d+)", result_cell)
        if not dish_match:
            continue
        dish_id = dish_match.group(1)

        craft_match = re.search(r'<span class="wh-icon-text"[^>]*data-type="number"[^>]*>(\d+)</span>', result_cell)
        craft_qty = int(craft_match.group(1)) if craft_match else 1

        spell_match = re.search(r"/spell=(\d+)", name_cell)
        spell_id = spell_match.group(1) if spell_match else ""

        reagents = []
        for chunk in re.finditer(r'<div class="iconmedium".*?</div>', reagent_cell, re.S):
            block = chunk.group(0)
            item_match = re.search(r"/item=(\d+)", block)
            if not item_match:
                continue
            qty_match = re.search(r'<span class="wh-icon-text"[^>]*data-type="number"[^>]*>(\d+)</span>', block)
            reagents.append((item_match.group(1), int(qty_match.group(1)) if qty_match else 1))

        skill_match = re.search(r"Кулинария\s*\(\s*(\d+)", strip_tags(skill_cell))
        skill = int(skill_match.group(1)) if skill_match else 0

        rows.append({
            "dish_id": dish_id,
            "spell_id": spell_id,
            "name": strip_tags(name_cell),
            "craft_qty": craft_qty,
            "skill": skill,
            "reagents": reagents,
        })
    return rows


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    text = read_pages()
    items = parse_item_data(text)
    log("[wowhead] предметов в addData: %d" % len(items))

    rows = parse_rows(text)
    log("[wowhead] строк рецептов: %d" % len(rows))

    by_dish = {}
    for row in rows:
        if row["dish_id"] in by_dish:
            continue
        by_dish[row["dish_id"]] = row

    with open(CACHE_FILE, "r", encoding="utf-8") as handle:
        cache = json.load(handle)

    def item_name(item_id, fallback=""):
        entry = items.get(str(item_id)) or {}
        return entry.get("name_ruru") or fallback or ("Item %s" % item_id)

    changes = []
    updated = []
    seen = set()
    missing = []

    for recipe in cache:
        dish_id = str(recipe.get("dish_id"))
        parsed = by_dish.get(dish_id)
        if not parsed:
            missing.append(dish_id)
            updated.append(recipe)
            continue
        seen.add(dish_id)

        new_reagents = []
        for rid, qty in parsed["reagents"]:
            existing = next((r for r in recipe.get("reagents", []) if str(r.get("id")) == rid), None)
            name = item_name(rid, existing.get("name") if existing else "")
            new_reagents.append({"id": rid, "name": name, "qty": qty})

        old_qty = {str(r["id"]): int(r.get("qty") or 1) for r in recipe.get("reagents", [])}
        new_qty = {r["id"]: r["qty"] for r in new_reagents}
        if old_qty != new_qty:
            changes.append("%s: реагенты %s -> %s" % (
                recipe.get("dish_name"),
                ", ".join("%s x%d" % (item_name(k, k), v) for k, v in old_qty.items()) or "—",
                ", ".join("%s x%d" % (r["name"], r["qty"]) for r in new_reagents) or "—",
            ))
        if int(recipe.get("craft_qty") or 1) != parsed["craft_qty"]:
            changes.append("%s: порций за крафт %s -> %s" % (
                recipe.get("dish_name"), recipe.get("craft_qty") or 1, parsed["craft_qty"]))
        if int(recipe.get("skill") or 0) != parsed["skill"]:
            changes.append("%s: навык %s -> %s" % (recipe.get("dish_name"), recipe.get("skill"), parsed["skill"]))

        recipe = dict(recipe)
        recipe["reagents"] = new_reagents
        recipe["craft_qty"] = parsed["craft_qty"]
        recipe["skill"] = parsed["skill"]
        recipe["spell_id"] = parsed["spell_id"]
        updated.append(recipe)

    # Рецепты, которых не было в кэше, но они есть в HTML
    for dish_id, parsed in by_dish.items():
        if dish_id in seen:
            continue
        entry = items.get(dish_id) or {}
        updated.append({
            "dish_id": dish_id,
            "dish_name": entry.get("name_ruru") or parsed["name"],
            "skill": parsed["skill"],
            "craft_qty": parsed["craft_qty"],
            "vendor_copper": int((entry.get("jsonequip") or {}).get("sellprice") or 0),
            "spell_id": parsed["spell_id"],
            "reagents": [{"id": rid, "name": item_name(rid), "qty": qty} for rid, qty in parsed["reagents"]],
        })

    with open(CACHE_FILE, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(updated, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    log("[recipes] записано рецептов: %d" % len(updated))
    log("[recipes] изменений: %d" % len(changes))
    for line in changes:
        log("  * " + line)
    if missing:
        log("[recipes] нет строки в HTML для: %s" % ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
