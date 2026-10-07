# Бриф: швидше прийняття листа (для нового чату, 07.10.26)

Власник: «так» на пункти 1 і 2 нижче. Пункт 3 НЕ робити.

## Замір (dev, справжній лист із фантомної скриньки, тестова таблиця, 3 прогони)
Повторні прогони ~1.4–1.6 с; прод 07.10.26 — 2.3–3.5 с на лист (лог `Slow request: POST /mail/<id>/accept`).

| Фаза (`perf.span`, коміт fd4f2bd) | dev | Що |
|---|---|---|
| `accept:sheet-row` | 0.54–0.80 с | `_write_placeholder_row` → `append_mail_placeholder_row` (читання зони + batch_update) |
| `accept:mail-folder` | 0.49–0.56 с | `_move_letter_to_processed_folder` → IMAP `move_message_to_folder` |
| `accept:tab` | 0.22 с (холодний 0.82) | `_resolve_target_tab` → `latest_worksheet_on_or_before(open_spreadsheet())` |
| `accept:files` + `accept:commit` | < 0.15 с | |

Фази вже пишуться в лог — після встановлення 0.21.52 подивитись реальну розбивку на проді (MCP `kmill_log` за `accept:`).

## Зробити

**1. Поштова папка — у фоні.** `app/services/mail_accept.py::_move_letter_to_processed_folder` (кличеться з `_accept_files_and_commit` наприкінці, лише при повному прийнятті). Уже best-effort: збій = SyncLog, лист лишається у Вхідних.
- Окремий однопотоковий виконавець (як `sheet_writeback_pool`), задача бере `email_id` і ВЛАСНУ сесію (сесії SQLAlchemy не потокобезпечні), перечитує лист.
- **Гонка «прийняв → одразу Повернув»:** `restore_email` → `_unaccept_email` (`app/routers/mail.py`) повертає лист у Вхідні лише якщо `email.mailbox_folder` уже стоїть. Якщо фон ще не встиг — після відкату фон перенесе лист у «Скачено», а статус «нове». Рішення: фонова задача і `_unaccept_email` під ОДНИМ локом листа (`mail_accept._letter_lock(email_id)`); задача під локом перевіряє, що лист досі `прийнято`, без нерозібраних файлів і без `mailbox_folder`, і лише тоді переносить.
- Конвеєр (`accept_email_batch` → `_accept_batch_letters`) теж іде через `accept_letter` → отримує те саме автоматично.
- Тести: (а) прийняття не чекає IMAP (мок, що спить 2 с); (б) прийняв → повернув до фону → лист НЕ перенесено; (в) збій IMAP у фоні → SyncLog `mail_to_folder` error, як зараз; (г) `log_folder_move(... via=VIA_ACCEPT)` і `mailbox_moved_at` ставляться як раніше.
- Наживо на dev: `C:\Users\1\AppData\Local\Temp\claude\…\scratchpad\accept_timing.py <email_id> 3` (логін `claude_test`, пароль у пам'яті `live-sheet-test-fixture`) — прийняти/повернути справжній лист 59, фази з `%TEMP%\orderdesk_dev.log`. Перевірити, що лист у фантомній скриньці справді в «скачано прощитано» після прийняття і повернувся у Вхідні після відкату.

**2. Вкладка дня — пам'ятати ~60 с.** `_resolve_target_tab` (той самий файл). Кешувати НАЗВУ вкладки (не об'єкт gspread між потоками) на (день, ~60 с); worksheet брати `get_worksheet_by_name` з кешованого `open_spreadsheet`. Збій Google — як зараз (повертаємо сьогоднішню назву, рядок не пишеться, `sheet_row_pending` + повтор). Тест: два прийняття поспіль — один запит переліку вкладок.

**3. НЕ робити:** рядок у таблицю у фон. На ньому стоять `sheet_row_pending`/повтор (`mail_row_retry.py`), зведення в один рядок (`_finish_merged_row` перевіряє `row_number` синхронно) і захист від дублів.

## Стан гілки `resilience/etap-1` (НЕ запушено, НЕ випущено; прод 0.21.51)
- 79cb838 — **критичний**: чиста інсталяція створювала порожню схему (500 на всіх сторінках).
- f84929d — mypy-храповик (CI Tests був червоний на 0.21.50/0.21.51).
- 3130419, be29618 — щоденні знімки, `KuubMill.exe --list-snapshots/--restore-snapshot`, сторож бази на старті (вікно Так/Ні), навчання `scripts/dr_drill.py`, `DR_RUNBOOK.md`.
- fd4f2bd — фази прийняття в лозі.
- Реліз 0.21.52: власник сказав «потім». Перед тегом — скіл `kuubmill-release` (тепер із mypy; після пушу дивитись І Tests, І Release).

## Ворота
`ruff check .` → `mypy app --ignore-missing-imports` (= `.mypy-baseline`, зараз 170) → цільові тести пошти → повний pytest один раз наприкінці. Коміт з `Co-Authored-By`.
