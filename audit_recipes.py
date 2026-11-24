#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Сверка recipes_cache.json: отчёт об изменениях и проверка целостности."""

import json
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")


def load_old():
    result = subprocess.run(
        ["git", "show", "HEAD:recipes_cache.json"],
        cwd=r"C:\parser", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout.decode("utf-8"))


def main():
    new = json.load(open(r"C:\parser\recipes_cache.json", encoding="utf-8"))
    old = {str(r["dish_id"]): r for r in load_old()}
    print("рецептов: %d (было %d)" % (len(new), len(old)))
    print()

    changed = []
    problems = []
    for recipe in new:
        dish_id = str(recipe["dish_id"])
        prev = old.get(dish_id) or {}
        old_qty = {str(x["id"]): int(x.get("qty") or 1) for x in prev.get("reagents", [])}
        new_qty = {str(x["id"]): int(x.get("qty") or 1) for x in recipe.get("reagents", [])}
        diffs = []
        for rid in new_qty:
            if old_qty.get(rid) != new_qty[rid]:
                diffs.append("%s: %s -> %d" % (recipe["reagents"][[str(x["id"]) for x in recipe["reagents"]].index(rid)]["name"],
                                               old_qty.get(rid, "—"), new_qty[rid]))
        for rid in old_qty:
            if rid not in new_qty:
                diffs.append("удалён реагент %s" % rid)
        if int(prev.get("craft_qty") or 1) != int(recipe.get("craft_qty") or 1):
            diffs.append("порций за крафт: %s -> %s" % (prev.get("craft_qty") or 1, recipe.get("craft_qty")))
        if diffs:
            changed.append((recipe["dish_name"], int(recipe.get("skill") or 0), diffs))

        if not recipe.get("reagents"):
            problems.append("%s: нет реагентов" % recipe["dish_name"])
        if int(recipe.get("craft_qty") or 0) < 1:
            problems.append("%s: craft_qty < 1" % recipe["dish_name"])
        for reagent in recipe.get("reagents", []):
            if int(reagent.get("qty") or 0) < 1:
                problems.append("%s: qty < 1 у %s" % (recipe["dish_name"], reagent.get("name")))

    print("изменены количества/порции у %d рецептов:" % len(changed))
    for name, skill, diffs in sorted(changed, key=lambda row: row[1]):
        print("  * [%d] %s — %s" % (skill, name, "; ".join(diffs)))
    print()
    print("проблемы: %s" % ("нет" if not problems else "; ".join(problems)))
    print()

    unknown = [r for r in new if any(x["id"] in ("239016", "239017", "249796", "13756", "21024") for x in r["reagents"])]
    print("рецепты с реагентами без цены (вердикт «Проверить»):")
    for recipe in unknown:
        print("  * %s: %s" % (recipe["dish_name"], ", ".join("%s x%d" % (x["name"], x["qty"]) for x in recipe["reagents"])))


if __name__ == "__main__":
    main()
