# WoW Forever — кулинарный дашборд (EU PvP Орда)

Экономика кулинарии на основе TSM Public Data: себестоимость крафта, прибыль вендору,
прибыль на аукционе с 5% комиссией, ликвидность TSM и списки покупок для Auctionator.

Два режима:

* **локальный сервер** — `dashboard_server.py` (живые цены, кнопка обновления, автообновление каждые 3 часа);
* **статический сайт** — `docs/index.html` со вшитыми данными для GitHub Pages (работает без Python и без ПК).

## 1. Локальный запуск

```
start_dashboard.bat
```

или

```
python dashboard_server.py --port 8080
```

Открывается `http://127.0.0.1:8080`. Пока сервер запущен, цены TSM обновляются каждые 3 часа;
после каждого обновления пересобирается статическая версия и (если настроен git-remote)
уходит в GitHub. Отключить автопубликацию: `set DASHBOARD_AUTOPUBLISH=0`.

## 2. Статическая сборка

```
python build_static_site.py            # свежие цены TSM + сборка + push
python build_static_site.py --no-push  # только собрать docs/index.html
python build_static_site.py --no-refresh
```

Скрипт кладёт все данные (`window.__STATIC_DATA__`) прямо в `docs/index.html`:
таблица, спойлеры с сортировкой, зелёная подсветка прибыли, импорт в Auctionator
и выгрузка CSV работают без сервера. Кнопка «Обновить» заменена плашкой
«Автообновление каждые 3 часа • Последнее обновление: …».

Если TSM недоступен и кэша нет — сборка прерывается, чтобы не затереть рабочий сайт пустыми ценами.

## 3. Публикация на GitHub Pages

Репозиторий уже инициализирован и закоммичен (`main`). Осталось создать пустой репозиторий
и привязать его:

```bash
# вариант А — через GitHub CLI (если установлен):
gh repo create wow-forever-cooking --public --source=. --remote=origin --push

# вариант Б — вручную:
# 1) создайте пустой репозиторий на https://github.com/new (без README и лицензии)
git remote add origin https://github.com/ВАШ_ЛОГИН/wow-forever-cooking.git
git push -u origin main
```

Затем в настройках репозитория:

1. **Settings → Pages → Source**: `Deploy from a branch`, Branch: `main`, папка `/docs`, **Save**.
2. **Settings → Actions → General → Workflow permissions**: `Read and write permissions` (иначе бот не сможет пушить обновления).
3. Через 1–2 минуты сайт будет здесь: `https://ВАШ_ЛОГИН.github.io/wow-forever-cooking/`

## 4. Автообновление 24/7

* `.github/workflows/update_tsm.yml` — каждые 3 часа (`cron: '0 */3 * * *'`) запускается на
  серверах GitHub: скачивает TSM, вызывает `build_static_site.py --no-push` и коммитит
  `docs/index.html` в `main`. GitHub Pages публикует новый коммит сам.
* Можно запустить вручную: **Actions → Update TSM prices → Run workflow**.
* Локально то же самое делает `dashboard_server.py` после каждого обновления цен (если есть remote).

## Что на экране

| Колонка | Что означает |
|---|---|
| Себестоимость | сумма по реагентам, для каждого берётся минимум из цены вендора и аукциона (MinBuyout) |
| Вендор | сколько вендор платит за крафт (все порции сразу) |
| Прибыль вендору | выручка вендора − себестоимость, **зелёная строка = гарантированный плюс** |
| Аукцион | TSM MinBuyout за крафт |
| Прибыль на аукционе | выручка − 5% комиссии − себестоимость |
| Выкуп / ликвидность | реальные `saleRate`/`soldPerDay`, если TSM их отдаёт; иначе оценка по спреду `minBuyout/marketValue` и тренду `recent/historical` |
| Вердикт | «Вендору», «На аукцион», «Не крафтить», «Проверить» (нет цены реагента) |

Клик по строке (или ▶) раскрывает спойлер ингредиентов: количество, цена за штуку,
источник цены, сумма и сортировка по сумме / цене за штуку / количеству / имени.

## Auctionator

* **⇩** у блюда — список покупок его реагентов;
* **⇩** у ингредиента — закупка одного реагента;
* **«Импорт всех профитных ингредиентов (Auctionator)»** — реагенты всех блюд
  с положительной прибылью вендору, без дублей, количество суммируется.

В модалке: формат (по умолчанию `Название;;макс_цена_в_меди;количество`), число крафтов,
язык названий (ruRU / enUS). Список копируется в буфер; в игре:
**Auctionator → Покупки (Shopping) → Импорт → Ctrl+V → Импорт**.

Максимальная цена закупки — цена реагента +20% (`PRICE_NAP_FACTOR` в `dashboard_server.py`).

## Настройки и файлы

| Файл | Назначение |
|---|---|
| `dashboard_server.py` | локальный сервер, расчёты, TSM, фоновое обновление + автопубликация |
| `build_static_site.py` | сборка автономного `docs/index.html`, git-commit/push |
| `index.html` | шаблон интерфейса (общий для локальной и статической версии) |
| `docs/index.html` | готовый статический сайт для GitHub Pages |
| `vendor_prices.json` | цены вендора в меди (перечитывается на лету) |
| `recipes_cache.json` | база рецептов |
| `tsm_prices_cache.json` | кэш TSM (в git не хранится) |
| `.github/workflows/update_tsm.yml` | облачное обновление каждые 3 часа |

`AH_COMMISSION` (5%), `REFRESH_HOURS` (3), `TSM_REALM_CANDIDATES` — в `dashboard_server.py`.

## API локального сервера

| Эндпоинт | Назначение |
|---|---|
| `GET /api/data` | все блюда с расчётами |
| `GET/POST /api/update` | принудительное обновление TSM |
| `GET /api/status` | состояние фонового обновления |
| `GET /api/auctionator?scope=all\|dish\|reagent&id=&fmt=&qty=&lang=` | строка импорта |
| `GET /api/export.csv` | выгрузка таблицы в CSV |

## Почему нет «скорости продаж» в цифрах

TSM Public Data для `forever/eu/realm/pvp-horde` отдаёт только
`itemId, name, marketValue, minBuyout, recent, historical, updatedAt` — без
`saleRate`, `soldPerDay`, `quantity` и `numAuctions`. Ликвидность считается по прокси
(спред минимального лота к рыночной цене + тренд recent/historical), и это явно подписано
в интерфейсе. Если TSM добавит поля, дашборд подхватит их автоматически.
