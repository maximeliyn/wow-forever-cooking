#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Сборка автономной статической версии дашборда для GitHub Pages.

Что делает:
  1. обновляет цены TSM (можно отключить: --no-refresh, тогда берётся кэш);
  2. считает экономику тем же кодом, что и локальный сервер (dashboard_server);
  3. вшивает все данные прямо в docs/index.html (window.__STATIC_DATA__);
  4. при наличии git-remote делает commit + push в ветку main.

Примеры:
    python build_static_site.py               # обновить, собрать, запушить
    python build_static_site.py --no-push     # только собрать (так делает GitHub Actions)
    python build_static_site.py --no-refresh  # собрать на данных из tsm_prices_cache.json
"""

import argparse
import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILE = os.path.join(BASE_DIR, "index.html")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
OUT_FILE = os.path.join(DOCS_DIR, "index.html")
BRANCH = "main"


def log(message):
    print(message)


# ----------------------------------------------------------------------------
# Сборка HTML
# ----------------------------------------------------------------------------
def render_static_html(data):
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as handle:
        template = handle.read()

    payload = dict(data)
    payload.pop("generated_at", None)
    payload["status"] = {"state": "static", "next_refresh_at": None, "refresh_hours": (data.get("settings") or {}).get("refresh_hours", 3)}
    payload["static_build"] = True

    # "<" экранируем, чтобы данные никогда не смогли закрыть тег script
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    injection = (
        "<script>\n"
        "/* Автономная сборка для GitHub Pages: все данные вшиты внутрь страницы. */\n"
        "window.__STATIC__ = true;\n"
        "window.__STATIC_DATA__ = %s;\n"
        "</script>\n" % raw
    )

    if "<body>" not in template:
        raise RuntimeError("в index.html не найден тег <body>")
    html = template.replace("<body>", "<body>\n" + injection, 1)

    # На статике кнопка обновления не нужна — её включает бейдж «Автообновление каждые 3 часа»
    html = html.replace(
        '<title>WoW Forever — Кулинария (EU PvP Орда)</title>',
        '<title>WoW Forever — Кулинария (EU PvP Орда)</title>\n'
        '<meta name="description" content="Экономика кулинарии WoW Forever (EU PvP Орда): себестоимость, прибыль вендору и на аукционе, ликвидность TSM, импорт в Auctionator.">',
        1,
    )
    return html


def write_site(data, out_file=OUT_FILE):
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    html = render_static_html(data)
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(html)
    os.replace(tmp, out_file)

    # GitHub Pages: отключаем Jekyll, чтобы не трогал файлы
    nojekyll = os.path.join(os.path.dirname(out_file), ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w", encoding="utf-8").close()
    return out_file


# ----------------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------------
def _cache_updated_at():
    try:
        with open(os.path.join(BASE_DIR, "tsm_prices_cache.json"), "r", encoding="utf-8") as handle:
            return json.load(handle).get("updated_at") or "auto"
    except Exception:
        return "auto"


def git(*args, check=False):
    result = subprocess.run(
        ["git"] + list(args),
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout or "").strip()
    if check and result.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), output))
    return result.returncode, output


def publish(out_file=OUT_FILE, branch=BRANCH, message=None):
    """git add + commit + push. Не падает, если remote/авторизации нет."""
    code, output = git("rev-parse", "--is-inside-work-tree")
    if code != 0 or output != "true":
        log("[git] папка не является git-репозиторием — push пропущен")
        return False

    code, remotes = git("remote")
    if not remotes.strip():
        log("[git] remote не настроен — push пропущен (см. README, раздел GitHub Pages)")
        return False

    rel_out = os.path.relpath(out_file, BASE_DIR).replace("\\", "/")
    rel_nojekyll = os.path.relpath(os.path.join(os.path.dirname(out_file), ".nojekyll"), BASE_DIR).replace("\\", "/")
    git("add", rel_out, rel_nojekyll)

    code, staged = git("diff", "--cached", "--quiet")
    if code == 0:
        log("[git] изменений нет — commit не нужен")
        return False

    message = message or "Update TSM prices (%s)" % _cache_updated_at()
    code, output = git("commit", "-m", message)
    if code != 0:
        log("[git] commit не удался: %s" % output)
        return False
    log("[git] commit: %s" % message)

    code, output = git("push", "origin", branch)
    if code != 0:
        log("[git] push не удался: %s" % output)
        log("[git] проверьте авторизацию GitHub (git credential manager / PAT) и ветку %s" % branch)
        return False
    log("[git] push origin %s: ok" % branch)
    return True


# ----------------------------------------------------------------------------
# Публичный API для dashboard_server
# ----------------------------------------------------------------------------
def build_from_data(data, out_file=OUT_FILE):
    """Собрать docs/index.html из уже посчитанных данных (без скачивания TSM)."""
    path = write_site(data, out_file)
    log("[static] собран %s (блюд: %d)" % (path, len(data.get("dishes") or [])))
    return path


def publish_from_data(data, out_file=OUT_FILE):
    """Собрать и запушить (вызывается сервером после обновления TSM)."""
    path = build_from_data(data, out_file)
    publish(out_file)
    return path


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Сборка статического дашборда для GitHub Pages")
    parser.add_argument("--out", default=OUT_FILE, help="куда писать index.html (по умолчанию docs/index.html)")
    parser.add_argument("--no-refresh", action="store_true", help="не скачивать TSM, взять кэш")
    parser.add_argument("--no-push", action="store_true", help="не делать commit/push")
    parser.add_argument("--branch", default=BRANCH, help="ветка для push (по умолчанию main)")
    args = parser.parse_args()

    import dashboard_server as ds

    # CLI сам решает, когда публиковать: не даём серверному refresh() сделать это параллельно
    ds.AUTO_PUBLISH = False

    dash = ds.Dashboard()
    cached = dash.load_cache()

    if args.no_refresh:
        if cached is None:
            log("[TSM] кэша нет, а --no-refresh указан — собирать нечего, сайт не трогаю")
            return 2
        dash.rebuild()
    else:
        dash.rebuild()
        if not dash.refresh("static-build"):
            if cached is None:
                log("[TSM] свежие цены не получены и кэша нет — сайт не пересобран, оставляю прежний index.html")
                return 2
            log("[TSM] свежие цены не получены, беру кэш от %s" % (cached.get("updated_at") or "?"))

    data = dash.data
    stats = data.get("stats") or {}
    path = build_from_data(data, args.out)
    log("[static] профит вендору: %d блюд, лучший: %s (%s)" % (
        stats.get("vendor_profitable", 0),
        stats.get("best_name") or "—",
        ds.money_text(stats.get("best_profit") or 0),
    ))

    if not args.no_push:
        publish(args.out, args.branch)
    else:
        log("[git] push отключён (--no-push)")
    log("[static] готово: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
