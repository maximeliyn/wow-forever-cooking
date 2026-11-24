# -*- coding: utf-8 -*-
import json
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

names = ["Медвежья рулька", "Яичница из гигантских яиц", "Железная печь",
         "Отпадные химеровые отбивные Дирга", "Хаше из нетопырей", "Пир из пестрожабра"]
recipes = {r["dish_name"]: r for r in json.load(open(r"C:\parser\recipes_cache.json", encoding="utf-8"))}

for name in names:
    recipe = recipes[name]
    url = "https://nether.wowhead.com/forever/tooltip/spell/%s?locale=7" % recipe["spell_id"]
    data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))
    text = re.sub(r"<[^>]+>", " ", data.get("tooltip", ""))
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"Ингредиенты:(.*?)(?:Уровень предмета|Требуется|$)", text)
    parsed = ", ".join("%s x%d" % (x["name"], x["qty"]) for x in recipe["reagents"])
    print("==", name, "| craft", recipe["craft_qty"])
    print("  cached:", parsed)
    print("  wowhead:", (m.group(1).strip() if m else "?")[:400])
