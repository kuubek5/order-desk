# Архітектурне рев'ю бекенду KuubMill (read-only)

Дата: 2026-09-05. Обсяг: app/web.py, main.py, models.py, schema.py, db.py,
routers/{queue,orders,handout,deps}.py, services/{queue,handout,undo,focus,
shift,vyrobitok,operators}.py, queue_filters.py, export_scanner.py,
order_folder.py, perf.py, stats.py, business_day.py + tests (conftest,
route_inventory, frontend_assets + 6 найбільших файлів).

---

## 1. Розшарування routers/services

Сервісний шар (`app/services/*`, `app/queue_filters.py`) реально існує й
використовується — це не фікція. Але три найбільші роути лишаються
god-функціями з бізнес-логікою прямо в HTTP-шарі:

1. **`app/routers/queue.py:123-580` — `get_queue`.** ~450 рядків в одній
   функції: bucketing за датами (163-177), пріоритет overdue/date/period
   (180-200), пришпилення «мої зараз» з власним сортуванням (203-207,
   316-338), розділення лаб/пошта (300-312), побудова KPI (350-364), побудова
   query-string для полла (367-380). HIGH. Біль: жодного unit-тесту на саму
   функцію окремо неможливо написати без повного FastAPI-контексту (Depends,
   Session) — усе перевіряється лише через test_mail_queue_backend.py
   виконанням реального роуту. Рекомендація: винести бакетизацію +
   пріоритезацію overdue/date/period + пришпилення в
   `app/services/queue.py` як одну функцію
   `build_queue_view(db, user, filters) -> QueueView`, лишивши в роуті лише
   парсинг параметрів і рендер.
2. **`app/routers/orders.py:362-556` — `create_manual_order`.** ~195 рядків:
   валідація форми (list-parallel полів), захист від double-submit
   (fingerprint/monotonic, 471-476), побудова батчу `works`, виклик
   Google-запису, СТВОРЕННЯ Order + StatusEvent + ActionLog в циклі (508-535).
   HIGH. Це єдине місце, де одночасно змішані: HTTP-валідація, доменна
   логіка (яка з двох гілок client/lab), інтеграція з зовнішнім Sheets API,
   ORM-запис. Рекомендація: `services/orders.py::create_manual_batch(...)`,
   роут лишає тільки Form-парсинг і редіректи.
3. **`app/routers/handout.py:679-786` — `issue_handout_group`.** ~107 рядків:
   ре-деривація групи клієнта з БД (candidates → фільтр по даті → фільтр по
   дню → фільтр по статусу, 705-731), запис статусу+StatusEvent у циклі,
   виклик Google Sheets (`open_spreadsheet`, `clear_row_fills`) прямо в
   роуті. MEDIUM-HIGH. `app/services/handout.py` вже існує й має
   `handout_eligible_orders` тощо, але сам "видати групу" — ні. Рекомендація:
   перенести деривацію групи + запис у `services/handout.py::issue_group(...)`.
4. **`app/routers/handout.py:73-389` — `handout_context`.** ~316 рядків.
   Краще за (1)-(3) тим, що вже винесена в окрему функцію (а не залита в
   тіло роуту) й має свій docstring-контракт, але це все одно helper у
   `routers/`, не в `services/` — і містить нетривіальну доменну логіку
   (matching клієнтів, fuzzy-fallback `_client_id_for`, побудова прев'ю
   токенів). MEDIUM. Рекомендація: перенести в
   `app/services/handout.py::build_screen(...)`, роутер лишає лише HTTP.
5. **`app/routers/queue.py:94-120` — `live_sync_status` / `sum_units`.**
   LOW: дрібні, але це ще два приклади доменних обчислень (сума одиниць,
   інтерпретація стану синку), яким природне місце — `services/queue.py`,
   а не router-модуль.

**Кількісно:** у queue.py з 888 рядків ~460 (52%) — тіло `get_queue`; у
orders.py з 1074 рядків найбільший маршрут займає 195 рядків (18%); у
handout.py з 812 рядків два маршрути/хелпери займають ~420 рядків (52%).

---

## 2. Моделі (`app/models.py`, 954 рядки, 27 класів)

**Індекси.** `Order.archived_at` (154) і `Order.material_id` (124) —
індексовані. `Order.status`, `Order.sheet_tab`, `Order.client_name`,
`Order.work_order_no`, `Order.job_code`, `Order.sum3d_id` — **без індексів**.
LOW-MEDIUM: `queue.py:200-206` тягне ВСІ активні ордери одним запитом і
фільтрує в Python (свідоме рішення, задокументоване — бізнес-дата рахується
з `sheet_tab`, а не зі стовпця, тож індекс на status тут не рятує головний
запит). Але `handout.py:707` (`Order.client_name == client_name`) і
`archive.py`/`stats.py` (ймовірно фільтри по `status`) сканують без індексу.
При ~92 рядки/день і накопиченні за роки (десятки тисяч рядків) це
поступово подорожчає full table scan. Рекомендація: індекс на `status` і
складений `(client_name, status)` для видачі, коли БД перейде поза
одноразовий скан 3k-5k рядків.

**`server_default=func.now()` заборонено для локального часу** (CLAUDE.md
§14) — перевірено: `ShiftNote.created_at` (674), `ActionLog.created_at`
(245-252, `default=datetime.now`, БЕЗ server_default) і коментарі при
`Feedback`/`FeedbackImage` (692, 778) свідомо уникають цього. АЛЕ
`Order.created_at/updated_at` (128-131), `StatusEvent.occurred_at` (213),
`Comment.created_at` (271), `ReworkRecord.created_at` (294),
`EmailMessage.*` (368,381,391), `Attachment.created_at` (439),
`ClientSenderMemory` (473), `FurnaceReading.captured_at` (789) — усі на
`server_default=func.now()`, тобто пишуть UTC на SQLite. Це, схоже,
**навмисно** (ці поля — технічні мітки синку/аудиту, не «людський» час
зміни), але ніде explicitly не задокументовано ЧОМУ саме ці поля ОК, а
ShiftNote/ActionLog — ні; ризик, що наступний розробник додасть нове поле за
аналогією з `Order.created_at` і отримає той самий 3-годинний зсув, який уже
двічі кусав (ActionLog, ShiftNote). MEDIUM. Рекомендація: один коментар-
маркер у `db.py` або `models.py` з правилом «людський/операторський час —
без server_default; системний/синк — можна» + grep-тест-сторож, що ловить
нове поле з UTC-семантикою в назві (`_at` у ShiftNote/ActionLog-подібних
контекстах).

**Enum vs string.** Жодного `sqlalchemy.Enum` в кодовій базі — усі статуси,
`source`, `blame`, `kind` — вільні `String`. Це свідомий вибір (статуси
редагуються в Google Таблиці людьми, не контрольований словник), але означає
відсутність DB-рівня захисту від друкарської помилки в статусі
(`"видано "` з пробілом і `"видано"` — різні рядки). LOW: `app/statuses.py`
(не прочитано детально, але імпортується як `STATUSES`) імовірно є
Python-рівня "enum"-списком — вартувало б переконатись, що усі write-шляхи
через нього звіряються, а не хардкодять рядки (побіжно бачив
`status="прийнято" if work["sum3d_id"] else "нове"` у orders.py:508 —
хардкод рядка замість константи).

**Каскади.** `Order` каскадить `status_events`/`comments`/`rework_records`
(`cascade="all, delete-orphan"`, 182-189) — узгоджено з тим, що ордери
ніколи не видаляються фізично (лише `archived_at`), окрім явного
`delete_order` (orders.py:648) — варто перевірити (не встиг) чи цей
маршрут справді рідко використовується і чи не губить історію мовчки.
`ShiftNoteImage`/`FeedbackImage` каскадять з `ondelete="CASCADE"` на
FK-рівні (629, 752) — це DB-level, узгоджено з ORM-cascade вище.

**`OrderEvent`/операторська історія.** Немає єдиного класу `OrderEvent` —
історію несуть `StatusEvent`+`Comment`+`ReworkRecord`+`ActionLog`.
`operator_id` скрізь `nullable=True` — але це коректно: синк (`app/sync.py`)
пише `StatusEvent(actor="sync")` без operator_id для автоматичних переходів,
а всі ручні дії оператора (`handout.py`, `orders.py`, `services/undo.py`)
завжди передають `operator_id=user.id`. Вимога §10 («завжди видно, який
оператор») дотримана для дій людини; для автоматичних дій системи це й не
мало б бути оператором. Добре.

**Soft-delete консистентність.** `archived_at` — єдиний soft-delete прапор,
використовується послідовно в `queue.py` (SQL-фільтр `is_(None)` +
Python-фільтр за retention) і в `services/queue.py::order_is_archived`.
Один предикат на два місця — ризик розходження, як і зазначено в CLAUDE.md
§14 про `open_notes`/`open_note_count`; не встиг звірити byte-for-byte, чи
`archive.py`-роут використовує той самий `order_is_archived`, чи власну
копію умови — вартує окремої перевірки.

---

## 3. Схема / міграції (`app/schema.py`, 228 рядків)

**Висновок суперечить початковому припущенню задачі:** це НЕ «власна
міграція без Alembic» — це повноцінний **Alembic** (`migrations/`, 43
версійні файли, `render_as_batch=True` для SQLite ALTER TABLE) з
самописним **guard-шаром** навколо `alembic upgrade head`
(`ensure_schema()`, schema.py:130-228). GOOD, і зроблено вдумливо:
- Бекап перед КОЖНОЮ міграцією (`backup_database`, ротація 5 копій, сортує
  за mtime, а не іменем — явно зазначено чому).
- `_matches_models()` — фінальна звірка схема==моделі після міграції,
  інакше `RuntimeError` і зупинка старту (не мовчазний дрейф).
- Обробка легасі-баз, створених старим `create_all` без alembic_version
  (штампування «head» без реального прогону).
- Крокове (`iterate_revisions`, не одним `upgrade head`) застосування з
  перехопленням «already exists»/«duplicate column» для баз у проміжному
  стані.

Це фактично приклад ДЛЯ інших частин системи, не ризик. Єдине, що не
перевірено (не встиг) — чи є downgrade-скрипти в усіх 43 версіях, чи
частина `def downgrade(): pass`-заглушки (типово для однонапрямних
проєктів, low risk при однокористувацькому standalone-деплої).

**`app/db.py`** (39 рядків) — мінімалістичний, `WAL` + `busy_timeout=5000`
на конекшн-івенті (добре для розділу читання/запису при живому синку).
`expire_on_commit=False` на `SessionLocal` — свідомий вибір (об'єкти
лишаються юзабельні після commit без re-fetch), нормально для
однопроцесного standalone.

---

## 4. Сесія БД / фонові потоки

`get_session()` (db.py:26) — контекст-менеджер per-call з
commit/rollback/close, стандартний патерн. `get_db()` (routers/deps.py:58,
не прочитано тіло детально, але за сигнатурою) — FastAPI Depends-генератор,
імовірно обгортка над тим самим.

`app/web.py` реєструє **окремі `SessionLocal()`-виклики в кожному
background-воркері** (`_mail_sync_worker`, `_sheet_sync_worker`,
`_furnace_worker`, `_machine_worker`, `_folder_warm_worker`,
`_export_warm_worker`, `_shift_images_prune_worker`,
`_sheet_backup_worker`, `_feedback_push_retry_worker`,
`_monthly_backup_worker`, `_system_load_worker` — 656-628) — кожен свій
`stop_event`-based цикл, кожен відкриває/закриває сесію на тік, а не тримає
одну довгоживучу. Це узгоджено з CLAUDE.md §14 («синк іде через
`_run_mail_sync_owned_session`, зомбі лишають свідомо») і з правилом
«сесія БД на одному потоці» для VNC-знімків печей. GOOD патерн для
SQLite-мультипоточності (уникає одної сесії, розділюваної потоками).
`license_gate`-middleware (web.py:822) також відкриває власну `SessionLocal()`
поза Depends — задокументовано чому (DI недоступний у middleware). Не
перевірено детально, чи всі ці воркери гарантовано `db.close()` у
`finally` при винятку всередині тіка (миттєвий погляд на `_mail_sync_tick`/
`_sheet_sync_tick` показує, що вони приймають вже відкриту `db: Session`
ззовні — отже, закриття залежить від виклику навколо; варто окремо
перевірити на витік сесій при винятку в тіку, я цього не встиг протестувати
дій сценарієм).

---

## 5. N+1 у рендері черги / `_row_context`

**GOOD, вже виправлено й задокументовано.** `app/order_folder.py:285-442`
(`attach_export_folder_uris`, `attach_job_code_folder_uris`) — явно
батчовані: один запит на всі email-ордери разом (`selectinload`
attachments), один прохід кореня мережевої теки на весь пакет, замість
N round-trip'ів на рядок. Коментарі в коді прямо називають виміряну
регресію («18 звернень на рядок» до фіксу) і дату фіксу — це саме той
клас багів, що в CLAUDE.md §14 названий «N+1 на мережевій шарі», і тут він
вже усунутий у двох конкретних місцях.

`app/routers/orders.py:82-100` (`_row_context`) — легкий хелпер, що додає
лише `focused_ids(db, user)` (один запит) до контексту одного рядка;
використовується послідовно, є guard-тест
(`test_order_focus.py::test_every_row_render_passes_focused_ids`) —
приклад добре захищеного інваріанта.

`get_queue` (queue.py:200-206) використовує `selectinload(Order.material)`
для головного запиту — eager load є. **Але** `handout_context`
(handout.py:73-389) робить кілька окремих проходів на кожен рендер екрана
(matching клієнтів через `db.scalars(select(Client))` повний скан таблиці
клієнтів — 155-160, потім `StatusEvent`-запит по `issued_ids` — 108-116) —
не є N+1 в класичному сенсі (один запит на пакет, не на рядок), але немає
жодного eager-load `selectinload` на самому `eligible`-запиті
(`handout_eligible_orders`, не переглянуто тіло детально) — варто
перевірити, чи там `Order.material`/сортування не тягне лінивого
довантаження при рендері шаблону.

---

## 6. Обробка помилок (`app/web.py:757-905`)

**GOOD, продумано.** Глобальні `@app.exception_handler` для
`StarletteHTTPException` і базового `Exception` (780-799):
`_wants_html(request)` розрізняє HTMX-фрагмент (`HX-Request` header) від
повної сторінки й повертає JSON/raw для HTMX замість HTML-сторінки помилки
— саме та вимога з задачі («HTMX-фрагмент чи повна сторінка при 500»),
виконана явно й з поясненням чому (сторінка помилки в середині таблиці
зламала б розмітку). Необроблені винятки логуються (`logger.exception`)
перед `raise exc` (799) — трасування не губиться навіть коли HTMX-шлях
прокидає далі. Є власний `error.html` з кодом логу (`log_path`) для
підтримки. Middleware `log_slow_requests` (848-892) додає `Server-Timing`
header і кільцевий буфер `/diag/perf` — рідкісний рівень спостережності
для проєкту такого розміру.

---

## 7. Тести

**Швидкість.** Немає `tests/conftest.py` взагалі (перевірено —
відсутній файл). Натомість **48 з ~106 файлів** самостійно визначають
`create_engine("sqlite://"/"sqlite:///:memory:", poolclass=StaticPool)` +
`Base.metadata.create_all` — паттерн правильний (in-memory, ізольовано), але
**дубльований у 48 місцях замість одного fixture в conftest.py**. MEDIUM:
будь-яка майбутня зміна (напр. додати `event.listens_for` для PRAGMA, як у
`db.py`) вимагає правки 48 файлів або залишиться неузгодженою. Рекомендація:
`tests/conftest.py` з фікстурою `db_session`/`test_engine`, поступова
міграція файлів на неї.

**Холодний старт.** `pytest tests/test_route_inventory.py` (2 тривіальних
тести) — **7.8с**. Це імпорт-хвіст (`app.web` тягне за собою весь застосунок
— alembic, gspread, PIL, планувальники воркерів) відпрацьовує на кожен
pytest-процес один раз; для одного файлу в TDD-циклі це відчутна затримка.
LOW-MEDIUM: не критично для CI (повний прогін розкладає цю вартість на всі
тести), але вартий уваги, якщо цикл розробки — «один тест, рестарт».

**Ізоляція.** Мережевих викликів у тестах не знайдено (grep на
`imaplib.IMAP4(`, `smtplib.SMTP(`, `socket.create_connection`,
`gspread.service_account(` — 0 збігів); `monkeypatch` використовується 954
рази в тестовому наборі — інтенсивне й послідовне мокання зовнішніх меж
(IMAP/Sheets/VNC), відповідає застереженню CLAUDE.md §14 про раніше
спійманий «IMAP-тест у справжню мережу». GOOD.

**Покриття за модулями.** Явних прогалин по великих і критичних модулях:
`app/config.py`, `app/models.py` (нема прямих unit-тестів моделей —
приймається, оскільки все проходить через роутні тести), `app/statuses.py`,
`app/crypto.py` (лише непрямо, через test_backup.py/test_furnace.py — немає
власного test_crypto.py, хоча шифрування секретів — чутлива ділянка §7),
`app/windows_dpapi.py`, `app/platform_windows.py`, CLI-обгортки
(`create_user_cli.py`, `mail_sync_cli.py`, `sync_cli.py`) — операційні
скрипти, типово нижчий пріоритет, але DPAPI/crypto — MEDIUM ризик без
прямого тесту (є непряме покриття, але не цільове: не видно тесту на
`InvalidToken`-деградацію, яку CLAUDE.md §14 явно називає критичною
(«Зміна ключа шифрування → InvalidToken → get_setting деградує в None»)
— рекомендую dedicated `test_crypto.py` саме на цей сценарій.

**Два сторожі.** `test_route_inventory.py` (78 рядків) і
`test_frontend_assets.py` (288 рядків) — обидва прочитані повністю,
відповідають опису в CLAUDE.md §14 (знімок роутів; JS-синтаксис +
порядок `<script>` + подвійні оголошення). Добре задокументовані, з
чіткою інструкцією як оновити знімок свідомо. GOOD.

**Найбільші тестові файли** (test_mail_queue_backend.py 1870,
test_sync.py 1484, test_handout_routes.py 1327, test_sheet_writer.py 922,
test_furnace.py 825) — самі по собі кандидати на розбиття за симетрією з
код-файлами, які вони покривають, але це нижчий пріоритет, ніж розбиття
самого продакшн-коду.

---

## 8. Конфігурація

`app/config.py` (116 рядків) — **плоский модуль з `os.environ.get(...)` на
рівні модуля**, не pydantic `BaseSettings`. Немає валідації типів/значень
при старті (крім `DB_ENCRYPTION_KEY = os.environ["DB_ENCRYPTION_KEY"]`, яка
впаде з `KeyError`, якщо змінна відсутня — жорстко, але без приємного
повідомлення). LOW-MEDIUM: для проєкту такого розміру із секретами через
UI (§7, `settings_store.py`) — `os.environ`-конфіг лишається лише для
bootstrap-рівня (шлях бази, ключ шифрування), решта секретів вже винесена
в БД-шар шифрування — тобто ризик менший, ніж здається, бо `config.py` не
є основним джерелом секретів рантайму.

**Реальний баг, не пов'язаний з задачею аудиту, але знайдений по дорозі:**
`app/config.py:87-104` — змінна `MACHINE_PORTRAITS_PATH` присвоюється
**чотири рази поспіль** (рядки 87-88, 92-93, 97-98, 102-103) з ідентичним
значенням і ідентичним коментарем над кожним блоком — явний
copy-paste-дубль. LOW (нешкідливо, бо ідемпотентно), але засмічує файл і
свідчить про неуважний merge/правку. Рекомендація: прибрати 3 зайві копії.

---

## 9. Типізація / лінтери

`pyproject.toml`: `ruff` (E+F, line-length 110, E501 ігнорується — свідомо,
через докстрінги) — **блокуючий** у CI (`.github/workflows/tests.yml:26`,
`python -m ruff check .`, без `continue-on-error`). `mypy` —
**інформаційний** (`continue-on-error` неявно через коментар «26
pre-existing type errors across 4 files», `allow_untyped_defs=True` тощо) —
свідомо lenient-режим, задокументовано. **Немає `.pre-commit-config.yaml`**
— лінт/тайпчек ловляться лише в CI, не локально до коміту. LOW-MEDIUM:
для соло/дуо-розробки з описаним у CLAUDE.md §15 workflow («один блок = один
чат, коміт наприкінці») це прийнятний компроміс, але pre-commit дав би
швидший фідбек, ніж чекати CI.

---

## 10. Розмір і когезія файлів — топ-5 кандидатів на розбиття

1. **`app/routers/settings.py` — 2280 рядків, 45+ маршрутів.** Найбільший
   файл проєкту, змішує: загальні налаштування, IMAP, Google OAuth/Sheets
   config, матеріали/аліаси, mail-spool, CRUD печей, CRUD верстатів (+
   портрети), нотифікації, self-check, backup export/import, snapshot/
   download таблиці, update-check/install, CRUD користувачів, feedback,
   vyrobitok-pin, section-gates. Природні межі вже видно з префіксів URL:
   `settings_connections.py` (sheets/imap/oauth), `settings_materials.py`,
   `settings_furnaces.py`, `settings_machines.py`, `settings_backup.py`
   (export/import/snapshot/download), `settings_users.py`,
   `settings_update.py`, `settings_misc.py` (notifications/feedback/
   vyrobitok-pin/sections). HIGH пріоритет — найбільший ROI на читабельність.
2. **`app/routers/mail.py` — 1688 рядків.** Не переглянуто детально в цьому
   проході (поза списком задачі), але за розміром — другий кандидат;
   ймовірно поєднує тріаж, IMAP-синк-виклики, класифікацію, прийняття
   листів. Вартий окремого проходу для меж розбиття.
3. **`app/routers/orders.py` — 1074 рядки.** Природний поділ: паспорт-дії
   (sum3d-id/operator/cam-comment/status — 101-311), ручне додавання
   (312-556 — і так найбільший блок, кандидат на власний
   `orders_manual.py` або переїзд у `services/orders.py`), undo/redo/
   journal (781-1017 — кандидат на `orders_undo.py`, симетрично до вже
   існуючого `services/undo.py`), деталі/коментарі (931-1074).
4. **`app/machine_ocr.py` — 980 рядків** (поза списком задачі, помічено
   при огляді розмірів) — разом із задокументованою в CLAUDE.md §14
   складністю glyph-розпізнавання, ймовірний кандидат на розбиття
   OCR-парсингу окремо від VNC/скріншот-логіки.
5. **`app/models.py` — 954 рядки, 27 класів в одному файлі.** Свідомо
   «джерело правди в одному місці» (CLAUDE.md §10), і для проєкту такого
   розміру це не обов'язково проблема — але за доменами вже видно чіткі
   групи (Order+StatusEvent+ActionLog+Comment+ReworkRecord; Mail:
   EmailMessage+MailFilterRule+MailFilterCategory+Attachment+
   ClientSenderMemory; Shift: ShiftNote+ShiftNoteImage; Furnace/Machine;
   Vyrobitok). Якщо файл продовжить рости — розбиття на `models/order.py`,
   `models/mail.py`, `models/shift.py` тощо з реекспортом через
   `models/__init__.py` зберегло б «один імпорт `from app.models import X`»
   без монолітного файлу.

`app/web.py` (974) і `app/sync.py` (845) — теж великі, але вища когезія
(web.py — суто інфраструктура: воркери+middleware+lifespan; sync.py, не
переглянутий детально) — нижчий пріоритет розбиття, ніж settings.py.

---

## Що зроблено добре

1. **Alembic-міграції з продуманим guard-шаром** (`app/schema.py`) —
   бекап-перед-кожною-міграцією, крокове застосування з обробкою
   «already exists», фінальна звірка схема==моделі перед стартом. Це
   зразковий рівень обережності для standalone Windows-деплою з живими
   даними на одній машині.
2. **N+1 на мережевій шарі вже усунуто й задокументовано** конкретними
   вимірюваннями (`order_folder.py`) — приклад того, як інженерна команда
   вчиться на власних інцидентах і залишає слід у коді, а не лише в пам'яті.
3. **Обробка помилок з усвідомленням HTMX-контракту** (`web.py:772-799`) —
   рідкісна деталь, яку легко забути (повна HTML-сторінка помилки, вставлена
   в фрагмент, ламає розмітку) і яка тут явно вирішена й документована.
4. **Два тест-сторожі проти тихих регресій розбиття** (`route_inventory`,
   `frontend_assets`) — конкретний, вузько націлений захист рівно на ту
   категорію багів, що болить найбільше при рефакторингу (загублений
   роут, зламаний порядок `<script>`), а не загальний «більше тестів».
5. **Дисципліна навколо `server_default=func.now()`** — навіть там, де
   правило порушується (§2 знахідка), сам факт, що для ShiftNote/ActionLog
   воно свідомо НЕ застосовується з прямим поясненням у коментарі й двома
   задокументованими інцидентами — ознака команди, яка записує уроки, а не
   наступає на ті самі граблі мовчки.

---

## Підсумок серйозності

- HIGH (3): god-функція `get_queue`; god-функція `create_manual_order`;
  розмір `settings.py` (2280 рядків, 45+ маршрутів).
- MEDIUM (6): `issue_handout_group` бізнес-логіка в роуті;
  `handout_context` у router-модулі замість services; неоднозначне правило
  server_default=func.now() (документовано частково); відсутність
  `tests/conftest.py` (48× дублювання DB-фікстури); відсутність прямого
  тесту на crypto/InvalidToken-деградацію; відсутність pre-commit.
- LOW (3): відсутні індекси на `status`/`client_name`; хардкод рядкових
  статусів замість констант у місцях (orders.py:508); потрійний
  copy-paste `MACHINE_PORTRAITS_PATH` (config.py:87-103).
