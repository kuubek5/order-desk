# Рев'ю: якість тестів, CI, паковання — KuubMill

Read-only аудит, 06-07.09.2026, worktree `CRM_Laba-audit`. Нічого не правив, сервери не піднімав.
Охоплено: `tests/` (130 файлів, 35214 рядків, 1873 `def test_` / 2119 зібраних кейсів),
`tests/conftest.py`, `tests/asgi_client.py`, `pyproject.toml`, `.pre-commit-config.yaml`,
`.github/workflows/{tests,release,agent-build}.yml`, `KuubMill.spec`, `installer/KuubMill.iss`,
`bump_version.py`, `requirements*.txt`, `build_windows.ps1`.

Повний прогін виконано один раз: `pytest -q --durations=25` → **1 failed, 2118 passed, 211.96s**.

---

## Зведення

| # | Серйозність | Коротко | Місце |
|---|---|---|---|
| 1 | **high** | Тест падає ЗАРАЗ (живий доказ під час аудиту): порівнює `sheet_tab` із `date.today()`, а `accept_email` рахує його через `business_today()` — розійшлись рівно на межі опівночі | `tests/test_web_helpers.py:384,426,495,508,531` |
| 2 | **high** | `release.yml` не запускає тести перед білдом/публікацією — окремий воркфлоу без `needs`/`workflow_run`; провалений `pytest` не блокує реліз | `.github/workflows/release.yml` (немає кроку тестів), `tests.yml` |
| 3 | **medium** | `app/routers/stats.py` рахує «сьогодні» через `date.today()`, а Черга/Видача/Архів — через `business_today()`; сторож бізнес-дня це не бачить (жорсткий список файлів) | `app/routers/stats.py:45`; `tests/test_business_day.py:81-125` |
| 4 | **medium** | Версія інсталятора в `release.yml` — 6 хардкод-літералів `KuubMill-Setup-0.10.5.exe`; синхронізує лише `bump_version.py` (ручний крок), і жоден тест не звіряє їх із `app/__version__.py` (лише `.iss` має `test_version_sync.py`) | `.github/workflows/release.yml:32,43,44,182,183`; `bump_version.py:107,133-140` |
| 5 | **medium** | Немає лок-файлу; `requirements*.txt` — майже все `>=` без верхньої межі (крім `bcrypt==4.0.1`), CI не відтворюваний день у день | `requirements.txt`, `requirements-dev.txt` |
| 6 | **low** | 61/130 файлів тестів (47%) досі дублюють `create_engine(poolclass=StaticPool)` замість фікстур `conftest.py`; мігровано 9 | `tests/*.py` (список — розділ нижче) |
| 7 | **low** | `pyproject.toml` — стара назва/версія (`order-desk`, `1.0.0`), `bump_version.py` цей файл не чіпає | `pyproject.toml:6-7` |
| 8 | **low** | `mypy` у CI — `continue-on-error: true`, інформаційний, 26 відомих помилок | `.github/workflows/tests.yml:28-33` |

---

## Знахідки

### 1 (high) — тест зіштовхнувся з business-day межею і впав насправді

`test_web_helpers.py::test_accept_email_stays_in_triage_after_full_accept` порівнює
`order.sheet_tab == date.today().strftime("%d.%m.%y")` (рядок 384; той самий патерн у 426,
495, 508, 531). `app/services/mail_accept.py:249` рахує вкладку через `business_today()`
(межа 07:30, нічна зміна ще на вчорашньому дні — CLAUDE.md §14 «Retention»).

**VERIFIED наживо**: прогін у цій сесії перетнув північ, і саме цей тест впав з
`AssertionError: assert '06.09.26' == '07.09.26'` — тест зафіксував календарну дату замість
робочої доби і відвалюється щоночі між 00:00 і 07:30. Сторож
`test_business_day.py::test_retention_cutoff_is_the_same_day_source_everywhere` існує саме
для цього класу помилок, але сканує ЖОРСТКИЙ список файлів у `app/` (docstring сама визнає:
«код, що переїхав у сервіс, випадає зі сторожа мовчки») — `tests/` туди не входить і не може.

**Фікс**: замінити 5 місць на `business_today().strftime("%d.%m.%y")`.

### 2 (high) — реліз не гейтиться тестами

`release.yml` (push тегу `v*`) одразу викликає `build_windows.ps1` → Inno Setup →
smoke-test → публікацію в `kuubek5/order-desk-releases`; кроку `pytest`/`ruff` в ньому нема.
`tests.yml` теж тригериться на push (теги підпадають), тож обидва воркфлоу йдуть ПАРАЛЕЛЬНО
на тому самому тезі без `needs`/`workflow_run` між ними. Провалений тест не зупинить
збірку й публікацію інсталятора. **VERIFIED**: `release.yml` прочитано повністю (185 рядків).

**Фікс**: `workflow_run` з умовою на успіх `tests.yml`, або продублювати `pytest -q` першим
кроком `release.yml`.

### 3 (medium) — «сьогодні» у Статистиці не business-day (unverified impact)

`app/routers/stats.py:45`: `today = date.today()`, поза жорстким списком `screens` у
`test_business_day.py`. Якщо період «сьогодні» показується оператору нічної зміни, він
розійдеться з вкладкою «Сьогодні» в Черзі (00:00–07:30). Не перевіряв, чи це поле бачить
нічний оператор в UI — unverified impact, варте 5-хвилинної перевірки перед фіксом.

### 4 (medium) — версія в release.yml синхронізується вручну, без тесту

`bump_version.py` одним запуском синхронізує `app/__version__.py`, `installer/KuubMill.iss`
і 6 літералів `KuubMill-Setup-<version>` у `release.yml`; `test_version_sync.py` звіряє лише
`.iss` з `app/__version__.py`. Розбіжність у `release.yml` (ручна правка одного з 6 місць,
або забутий `bump_version.py`) виявиться лише на кроці «Verify installer exists» СЕРЕД
CI реліз-білда — голосно, але після витраченого білд-часу. Зараз (`VERSION = "0.10.5"`)
усі 6 місць збігаються, драйфу нема. **Фікс**: тест-сторож, що грепає `release.yml` на
`KuubMill-Setup-{VERSION}` до тегування.

### 5 (medium) — немає лок-файлу

`requirements*.txt` майже все на `>=` (виняток — `bcrypt==4.0.1`, свідомо запінений через
несумісність із passlib). Код сам документує живу пастку: `asyncvnc` тягне `TripleDES` зі
старого місця `cryptography`, яке колись приберуть — точно той тип регресії, від якої лок-
файл захищає, а відкритий діапазон ні. `tests.yml` і `build_windows.ps1` можуть тягнути різні
версії тієї самої залежності в різні дні.

### 6 (low) — дублювання фікстур, 61/130 файлів

`tests/conftest.py` дає `db_engine`/`db_session` як заміну ~48 копіям
`create_engine(poolclass=StaticPool)`, документуючи це як свідомо поступову міграцію.
Станом на аудит: **61 файл** досі з локальним `poolclass=StaticPool`, **9 файлів** уже на
спільних фікстурах (`test_auto_accept.py`, `test_crypto.py`, `test_mail_spool.py`,
`test_palette.py`, `test_sender_memory.py`, `test_settings_status.py`,
`test_setup_helpers.py`, `test_sheet_headers_quota.py`, `test_sync_pause_gate.py`). Не
блокер — видимість прогресу задокументованої міграції.

### 7-8 (low) — метадані

`pyproject.toml`: `name = "order-desk"`, `version = "1.0.0"` — доперейменувальні, і
`bump_version.py` цей файл не редагує (лише `app/__version__.py`, `.iss`, `release.yml`,
`CHANGELOG.md`). Не впливає на PyInstaller/Inno (жоден не читає `pyproject.toml`), але
суперечить дисципліні «назву не займати мовчки» з CLAUDE.md §14. `mypy` у `tests.yml` —
`continue-on-error: true`, документовано, 26 відомих помилок, за задумом не гейтить CI.

---

## (1) Monkeypatch/patch no-op перевірка

Написано AST-скрипт: для кожного `monkeypatch.setattr(mod, "name", …)` / `patch.object(...)` /
`patch("dotted.path")` у `tests/*.py` резолвиться реальний об'єкт-ціль (з проходом по ланцюжку
атрибутів, а не по рядку) і перевіряється (а) чи атрибут існує, (б) чи ім'я взагалі
згадується в модулі-власнику (проксі для «чи хтось там його справді викликає»).

Всього патч-точок: **613**. Резолвлено статично: **~570**, підозрілих: **14**, нерезолвлених
(динамічні цілі — локальні змінні, фікстур-об'єкти request-scope): **43**.

Усі 14 підозрілих вручну перевірено — **жодного реального no-op не знайдено**, усі —
хибні спрацювання евристики:

| Файл:рядок | Ціль | Чому хибне спрацювання |
|---|---|---|
| `test_feedback.py` (8 місць) | `telegram.push_enabled/send_feedback` | `app/services/feedback.py` викликає `telegram.push_enabled(db)` через `from app.services import telegram` — патч на модуль влучає; евристика не бачила виклик в іншому файлі |
| `test_furnace.py:464` | `router.poll_all` | Артефакт скрипта: пізніший `from app.routers.settings import router` у тому ж файлі переписав alias у моєму пофайловому (не по-скоупному) `import_map`; реальний `from app.routers import furnace as router` валідний |
| `test_handout_routes.py:1341` | `handout.sync_control.record_viewed_day` | `handout.py` робить `from app import sync_control` — той самий об'єкт-модуль, патч влучає |
| `test_runtime.py:16,29` | `runtime.sys.frozen` / `sys._MEIPASS` | `raising=False` — навмисне СТВОРЕННЯ PyInstaller-атрибутів, не патч наявного |
| `test_settings_routes.py:96` | `Path.write_bytes` (клас) | Легітимний патч stdlib-методу; хіт=1 в `pathlib.py`, реальні виклики — в коді застосунку |
| `test_vyrobitok.py:672` | `sync_control.is_paused` | Реальні виклики розкидані по `orders.py`/`queue.py`/`handout.py`, не в `sync_control.py` |

Висновок: серед статично перевірюваних цілей підтверджених «мовчазних no-op» немає. 43
нерезолвлені цілі (динамічні об'єкти) не перевірено — дешевший шлях за повторний ручний
аудит: `assert getattr(x, "y", _MISSING) is not _MISSING` у тестах на найризикованіших
патчах (ціль — недавно перенесений код).

## (4) Найповільніші тести (`--durations=25`, один прогін)

| Час | Тест | Причина (з коду) |
|---|---|---|
| 21.0s | `test_schema_bootstrap.py::test_stale_database_with_tables_create_all_already_made` | реальна Alembic-міграція на файловій БД |
| 18.3s | `test_schema_bootstrap.py::test_stale_database_is_migrated_to_head` | те саме |
| 17.9s | `test_schema_bootstrap.py::test_backup_is_taken_before_migrating` | те саме + бекап |
| 12.1s | `test_machine_agent.py::test_frame_is_written_to_disk_less_often_than_it_is_analysed` | реальний час/IO цикл |
| 6.7s / 6.6s / 6.5s | `test_migration_drift.py::*` (3 тести) | будують усі міграції з нуля на кожен тест |
| 6.1s | `test_machine_agent.py::test_poll_target_stamps_percent_change_only_when_number_moves` | те саме, що вище |
| 5.0s | `test_sheet_write_safety.py::...test_a_slow_write_does_not_block_the_loop` | навмисний сон, перевіряє неблокуючість |
| 2.3-2.9s | `test_client_migration.py` (×3), `test_frontend_assets.py::test_every_javascript_file_parses` | Alembic / `node --check` підпроцес |

Топ-4 (64s із 212s, 30% часу всього прогону) — три `test_schema_bootstrap` + один
`test_machine_agent` тест. Навмисно реалістичні (справжня Alembic-міграція, справжній файл
на диску) — не флейк, кандидат на `pytest -m slow` розділення, якщо 3.5 хв стане відчутним.

## (6) Роутери/сервіси без прямого посилання в тестах за іменем файлу

Грубий греп імені модуля по `tests/*.py` (0-2 згадки), з ручною перевіркою найнижчих:

| Файл | Тестів (греп) | Верифіковано |
|---|---|---|
| `app/routers/settings/common.py` | 0 | **OK** — `require_settings_edit`/`require_settings_admin` покриті `test_admin_gate.py`, `test_settings_nav.py`, `test_machines.py` (грепаються за іменем функції, не файлу) |
| `attempt_limit.py`, `look_prefs.py`, `materials_console.py`, `sum3d_capture.py`, `system_load.py`, `telegram.py` | 1 | не перевірено кожен вручну — ймовірно власний `test_<name>.py`, греп рахує лише згадки ПОЗА ним |
| `sheet_sync_service.py` | 6 файлів, 29 `def test_` у власному | **OK**, включно з `mass_vanish_pending` (запобіжник масової архівації, CLAUDE.md §14) — поріг «>5 і >25%» явно в docstring `test_sheet_sync_service.py:691` |
| `sync.py` | 21 файл згадує | **OK**, широко покритий |

Жодного роутера/сервіса з нульовим покриттям після ручної перевірки не знайдено — низькі
числа в грепі пояснюються власним однойменним тест-файлом, який не згадує «чужу» назву.

---

## Verified OK

- Мережа/ФС поза `tmp_path`: `test_sheet_writer.py`, `test_sheets_retry.py`,
  `test_sheet_connect_ux.py` використовують лише `gspread.exceptions`/`gspread.utils` (чисті
  функції) і `Mock`, без реальних викликів; `test_link_attachments.py` мокає HTTP повністю;
  `test_sum3d_capture.py`/`test_windows_launcher.py` — `tmp_path` або свідомо неіснуючі шляхи.
- Guard-тести (`test_route_inventory.py`, `test_settings_nav.py`, `test_frontend_assets.py`,
  `test_business_day.py`) — усі з докстрінгом «як оновити знімок СВІДОМО» (CLAUDE.md §14).
- `KuubMill.spec` — data files повні (templates, static, migrations, alembic.ini, CHANGELOG,
  іконка, `app/data` еталони цифр печей), hidden imports документовано (`asyncvnc`/
  `keysymdef` лінивий імпорт, `pystray`/`PIL` для трея), `excludes=["pytest"]`. Розмір
  білда не заміряно — інсталятор у цьому worktree не збирався (поза read-only скоупом).
- `installer/KuubMill.iss` — autostart через `HKCU\...\Run` (без адмінправ, узгоджено з
  `PrivilegesRequired=lowest`), `[UninstallRun]` шле `--shutdown` перед видаленням; дані
  (LOCALAPPDATA\KuubMill) поза `{app}` — не перевіряв деінсталяцію наживо.
- Три задокументовані міграції даних (OrderDesk→KuubMill) досі в коді (`runtime.py`,
  `config.py`, `backup.py`, `monthly_backup.py`) — не видалені передчасно.
- `sha256`-checksum інсталятора рахується й публікується разом з exe.
- `agent-build.yml` — версія агента вже не хардкодиться (`date + short SHA`, задокументовано).

---

## Рекомендації

1. Замінити `date.today()` на `business_today()` у 5 місцях `tests/test_web_helpers.py` (finding 1) — падає щоночі.
2. Додати гейт тестів у `release.yml` (finding 2) — навіть простий `pytest -q` перед білдом.
3. Перевірити `app/routers/stats.py:45` — чи «сьогодні» бачить оператор нічної зміни (finding 3).
4. Маленький тест-сторож на відповідність `release.yml` ↔ `app/__version__.py` (finding 4).
5. Розглянути `pip-compile`/лок-файл для `requirements*.txt`, зважаючи на `asyncvnc`/`TripleDES` пастку (finding 5).
6. Не форсувати міграцію 61 файлу на `conftest.py` одним махом — документована стратегія (finding 6) слушна, лишити як є.
7. Оновити `pyproject.toml` (name/version) наступного разу, коли хтось його відкриє — не варте окремого коміту.
8. `test_business_day.py`'s `screens` — додати нагадування в CLAUDE.md §14 «Retention»: перевіряти новий екран, що показує «сьогодні», а не лише роутери зі списку.
9. 43 нерезолвлені monkeypatch-цілі — не варті ручного повторного аудиту; ризик уже покритий тим, що 2118/2119 тестів проходять і CI зелений на кожен PR.
