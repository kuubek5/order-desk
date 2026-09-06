# Ревʼю області «домен оператора» — 07.09.26

Черга / наряди / undo / видача / клієнти / архів / статистика / Виробіток / зміна.
Зібрано з чотирьох суб-ревʼю (кожен кластер читався цілком). VERIFIED = прочитано код; те, що
позначено «виправлено», уже в master (коміт `dedecfc`).

## CRITICAL
- `app/routers/orders.py:754` — `redo_last_action` був `async def`, а `perform_redo` пише в таблицю синхронно (gspread) → event loop. Дзеркальний `undo_action` — звичайний `def`. **Виправлено** (plain def).
- `app/services/handout.py:330-357, 376-400` — `mark_group_found` та `issue_group` фільтрували `d < today`, тоді як `handout_eligible_orders` (:75-112, фікс ef053c9 31.08) включає сьогодні (ПММА/титан/віск готові в день). «Усі знайдено»/«Видати N з M» мовчки пропускали сьогоднішні роботи; при фільтрі дня = сьогодні кнопки були мертві. **Виправлено** (`<= today`, тест оновлено).

## HIGH
- `app/routers/orders.py:709,731,765` — вікно undo: `ActionLog.created_at` — локальний час (`datetime.now`, без `server_default`), а cutoff = `utc_now()` → «5 хвилин» тривали 3 год 05 хв (виміряно: 10800 с). **Виправлено** (`datetime.now()`).
- `app/routers/stats.py:45` — `date.today()` як межа періоду, тоді як `order_date()` — по робочій добі; між 00:00 і 07:30 «Сьогодні/Тиждень» зсунуті на день. **Виправлено** (`business_today()`).
- `app/routers/vyrobitok.py:205-214` ↔ `app/web.py:324-336` — `unfreeze_day` → синк (секунди) без координації з фоновим заморожувачем (кожні 10 хв): день міг заморозитись зі старих даних ДО коміту синку, ресинк мовчки губився. **Виправлено** (`resync_guard(day)`, заморожувач пропускає день).
- `app/routers/handout.py:799-825` — `POST /handout/confirm-alias` без жодного посилання з UI; дубль `bind_client_folder` (clients.py:268). **Видалено**.

## MEDIUM
- `app/services/vyrobitok.py:513-518` — `disks == 0` при `zr_total > 0` → `DEFAULT_RATE_ZN` без попередження (банер `rate_out_of_band` спрацьовує лише коли коефіцієнт існує). Додати попередження «диски не заповнені».
- `app/services/queue_view.py:148-625` — `build_queue_view` ~480 рядків; `app/routers/handout.py:81-405` — `handout_context` ~325. Не баг, а поріг 150.
- `app/routers/queue.py:270-299` — `/search` вантажить усі не-архівні Order; `attach_*` пакетні, ок при поточних обсягах.
- `app/routers/handout.py:578` — `_handout_pulse` — глобал без евікшену (ключ `(user.id, day)`).

## LOW
- `app/services/undo.py` — TOCTOU: два кліки на один `ActionLog` до коміту → два записи undo (не корупція).
- `app/statuses.py:69` — імпорт `business_today` у тілі функції (циклічний імпорт) без коментаря.
- `app/services/order_dates.py:33` — `date.today()` лише як ±5 років санітарна межа року — нешкідливо.

## Verified OK
- Порядок видачі `(order_date, row_number)` — статичний, полли не пересортовують; без авто-зіставлення (лише кандидати); два кроки без зайвих підтверджень (QC-чеклист — свідомий виняток, вимкнено за замовчуванням).
- `StatusEvent` + `actor` на КОЖНІЙ мутації (status, sum3d auto-advance, delete, change-seen, undo/redo, manual add); жодного hard delete Order/StatusEvent/ReworkRecord.
- Записи в таблицю: `await await_on_writeback` у async-роутах, `submit_sheet_write(...).result()` у `create_manual_order`, `*_background` для fire-and-forget; `clear_order_row` через identity; пауза синку в 10 місцях orders.py.
- `business_today()` у queue/handout/archive/orders/vyrobitok; `mine=1` у `rows_qs`; `focus_ranks` один раз; нова шпилька в кінець набору (рядки не рухаються).
- Shift: `open_notes()`/`open_note_count()` — один предикат; три FK на users з `foreign_keys=[...]`; `datetime.now()` без `func.now()`.
- `shift_images`: symlink/junction на кожному сегменті, whitelist форматів від Pillow, SVG виключено, `.part` прибирається на всіх гілках.
- Export: скан паралельний (16 потоків) + кеш, UNC↔літера через `resolve()`+`relative_to()`, суфікси тек `(2)` підхоплюються; `match_client_name` — підтверджені пари ПЕРЕД fuzzy, поріг 90 + запас 5, до 3 кандидатів.
- СЛМ у Виробітку — за текстом, не кольором; freeze/unfreeze не чіпає `override_value`; PIN — `secrets.compare_digest`.
- Жодного застарілого monkeypatch (усі цілі — там, де символ читається).
