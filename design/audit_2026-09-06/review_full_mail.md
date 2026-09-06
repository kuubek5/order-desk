# Ревʼю модуля «Пошта» (end-to-end), 06.09.26

Область: mail_reader / mail_parser / mail_sync_service / mail_export / mail_spool /
link_attachments / archive_extract / triage_status / material_classifier / client_matcher /
order_folder / routers/mail.py / services/mail_accept.py / services/materials_console.py /
mail-моделі / шаблони mail_*.

Критерій оцінки: **файл клієнта не може загубитись або причепитись до чужої роботи.**
Усі знахідки VERIFIED читанням коду; позначені `(прогін)` підтверджені виконанням.

## CRITICAL

- `app/mail_reader.py:499-503` — `download_attachments_now` тягне лист `AND(uid=...)` БЕЗ звірки UIDVALIDITY теки. Після перестворення теки той самий uid належить чужому листу — вкладення стороннього клієнта чіпляються до цього EmailMessage. `fetch_new_emails:657-666` цей самий випадок явно ловить і пропускає. — Читати `_folder_uidvalidity(mailbox)` і відмовляти, якщо `email.uid_validity` не збігається.
- `app/mail_reader.py:471-483` — те саме в `redownload_missing_attachments`, і тут гірше: рядки `Attachment` СПОЧАТКУ видаляються (`session.delete` + `flush`), і лише потім іде фетч. При зміні UIDVALIDITY кнопка «скачати наново» замінює STL клієнта чужими, без сліду. — Той самий гейт, і перевіряти ДО видалення рядків.
- `app/client_matcher.py:133-150` — префільтр `_shortlist` рахує `token_set_ratio` по НЕтранслітерованих рядках; при >40 тек кирилиця проти латиниці дає ~0 і правильна тека відсіюється до дорогого скорера. `(прогін)`: «Мулик Петро» проти теки «Mulyk Petro» серед 60 тек → `matched_folder_name=None`, хоча `_score_pair`=100. Ламає і видачу, і `mail_export._resolve_client_folder_name` (прийняття форкає НОВУ теку клієнта замість наявної). — Префільтрувати по транслітерованих варіантах, exact/translit-exact лишати поза відсівом.
- `app/routers/mail.py:1554-1555` (`_unaccept_email`) — «Повернути в тріаж» ВИДАЛЯЄ всі `Order` листа незалежно від їхнього статусу: відфрезеровану чи вже видану роботу зносить разом із `StatusEvent`/`Comment`/`ReworkRecord` (cascade delete-orphan, models.py:196-203). Без підтвердження. CLAUDE.md §5 вимагає точної історії «хто що зробив». — Відмовляти для робіт далі «прораховано», решту через підтвердження.
- `app/routers/mail.py:1562-1584` — `restore_email` не має жодного гейта прав (лише факт входу), тоді як усі роути фільтрів мають `can_edit`. Разом із попереднім пунктом це «видалити роботу з історією» одним POST. — Мінімум `can_edit`.

## HIGH

- `app/mail_sync_service.py:121-133` + `:211-212` — після `MailSyncTimeoutError` `_sync_lock` віддається у `finally`, а зомбі-потік ще тримає сесію й ПИШЕ файли. Наступний тік бере лок і стартує ДРУГИЙ фетч у ті самі `mail_attachments/<uid>/`: два `unique_destination` без синхронізації → «файл» і «файл (2)», подвійні рядки Attachment. — Не звільняти лок, поки зомбі живий (`worker.is_alive()`), або окремий стан «drain».
- `app/services/mail_accept.py:153-156, 178-181` — «нерозібрані» і «залишок» визначаються `Path(a.saved_path).exists()`. На UNC (`\\host\share`) моргання шари дає False → файл НЕ переїжджає в export, лишається в спулі з `order_id=None`, а лист позначається «прийнято». Це рівно той асиметричний ризик, заради якого написано `mail_reader._file_is_missing` (ретраї, «втрата» лише на чистому FileNotFoundError) — і тут він не використаний. — Викликати `_file_is_missing`, а не `exists()`.
- `app/mail_reader.py:368` — тека спула зветься `attachments_dir / email_message.uid`, тобто лише uid, тоді як ключ листа — пара `(uid, uid_validity)` (models.py:418-420). Після зміни UIDVALIDITY два РІЗНІ листи ділять одну теку; `mail_spool.analyze_spool:87-91` цю двозначність уже обходить коментарем замість виправлення. — Іменувати `<uid_validity>_<uid>` з міграцією наявних.
- `app/routers/mail.py:221` — `attach_email_preview_tokens` викликається ДО гілки `partial == "list"`, тобто на КОЖНОМУ 15-секундному поллі для кожного листа. `order_folder.resolve_email_attachment_folder:173-231` робить на кожне вкладення `resolve(strict=True)`, `is_link` по кожному сегменту шляху й `is_file`/`is_dir` — сотні round-trip'ів на мережеву шару за тік (CLAUDE.md §14, «N+1 на мережевій шарі»). Кешу, як `_tech_root_children` для тек техніків, тут немає. — Не рахувати токени для полла або кешувати з TTL.
- `app/routers/settings/overview.py:329` — `analyze_spool` на КОЖНОМУ рендері `/settings`: `_dir_size` робить `rglob("*")` по всіх теках спула плюс повне читання таблиці `email_messages`. Двома рядками нижче коментар пишається, що важкий обхід export тримають поза завантаженням сторінки. — За кнопкою або з кешем.
- `app/mail_parser.py:318` + `:66-69` — `client_name_guess` беруть з будь-якого рядка `^From:|Від:|От кого:` у ТІЛІ, не лише в пересланих листах. `(прогін)`: звичайна відповідь клієнта з цитатою нашого листа дає `client_name_guess='lab@kuubmill.local'`. Це ж значення тече в `sender_memory.sender_key_for` і в ключ памʼяті відправника. — Застосовувати лише коли субʼєкт має Fwd-префікс (умова вже є в `sender_memory._is_forwarded`).
- `app/mail_reader.py:224` — `existing` збирається з `email_message.attachments`, але нові рядки додаються через `session.add(Attachment(email_message_id=...))` без flush, тож колекція їх не бачить. Лист із ДВОМА архівами, що містять однойменні файли, розпакується з дублікатами. — `session.flush()` у циклі або накопичувати імена локально.

## MEDIUM

- `app/routers/mail.py:1520` — `_unaccept_email` бере ВСІ вкладення, зокрема ті, що ніколи не виїжджали зі спула; `restore_attachments_to_spool` перейменовує їх у « (2)». `(прогін)`: `crown.stl` → `crown (2).stl`. Кожен відкат частково прийнятого листа множить суфікс. — Брати лише `order_id is not None`.
- `app/models.py:571` — `Attachment.staged_to_export` НІДЕ не ставиться в True (лише читається у 5 місцях і скидається у 2). Отже `mail_accept.py:279-280`, `routers/mail.py:1077-1089` («повернути авто-викладені файли зі спула») і `staged_count` у панелі — мертвий код, що імітує наявну фічу. — Видалити або дописати виставлення.
- `app/routers/mail.py:948-950` — докстрінг і вкладка «Авто-прийняття» обіцяють автоматичне ПРИЙНЯТТЯ листа; `auto_accept` читається лише в `sender_memory.is_auto_sender`, тобто керує авто-СКАЧУВАННЯМ. Оператор вірить у неіснуючу автоматику. — Перейменувати або реалізувати.
- `app/services/mail_accept.py:145` — `email.order_id = new_order.id` переписується на кожній кольоровій партії, тож лист памʼятає лише ОСТАННЮ роботу; звʼязок із попередніми тримає тільки `Order.source_email_id`, а legacy-гілка `_unaccept_email:1515-1518` спирається саме на це поле. — Не писати `order_id` при частковому прийнятті.
- `app/services/mail_accept.py:78-230` — жодного захисту від двох операторів (§1: їх якраз двоє). Гейт `email.status != "нове"` стоїть у роуті ДО виклику, між ними немає блокування рядка → два `Order` на один лист. — Блокування рядка або унікальний ключ на партію.
- `app/material_classifier.py:63-67, 87` — token-аліаси `500/800/1000/2000/st/tr/nat/zr/ti` ганяються по ВІЛЬНОМУ тексту листа (`mail_parser:308-312`). `(прогін)`: «оплата 500 грн» → Цирконій, «Nature of the request» → Цирконій, «Ti amo» → Титан. — Для вільного тексту вимикати `token`-режим (він писався під коротку клітинку «Колір роботи»).
- `app/material_classifier.py:115-122` — `_FALSE_FRIEND_RE` покриває `времени/ем/ах/у`, але не `времена`. `(прогін)`: «времена змінились» → ПММА. — `времен\w*` замість переліку відмінків.
- `app/mail_parser.py:47-56` — `_NOT_LETTER` не рахує цифри й `@` за літери. `(прогін)`: «emo@clinic.com» і «emo123» дають матеріал «emo». — Розширити межу до `[^\W_]`.
- `app/link_attachments.py:196-214` — `_stream_to_file` прибирає файл лише на `LinkDownloadError`; обрив мережі / `ChunkedEncodingError` посеред потоку лишає ОБРІЗАНИЙ файл у спулі. Рядка Attachment не буде (`routers/mail.py:547-552`), тож файл невидимий у базі, але лежить поруч зі справжніми й потрапляє в «Відкрити папку». — `except BaseException: dest.unlink(); raise`.
- `app/routers/mail.py:392-394, 443-445, 411` — три окремі проходи по диску над тими самими вкладеннями в одному рендері панелі (`_email_partial_state`, `missing_attachment_ids`, `attach_email_preview_tokens`), плюс ще два з `triage_status.triage_readiness`/`files_on_disk` у шаблоні списку. — Один прохід, результат у контекст.
- `app/routers/mail.py:1186-1207, 914-959` — `filter_email_manually`, `add_sender_auto`, `toggle_sender_auto` без `can_edit`, тоді як усі сусідні роути фільтрів його мають. Додавання довіреного відправника вмикає авто-скачування вкладень з адреси. — Узгодити гейти.
- `app/routers/mail.py:107-332` — `get_mail` ~230 рядків: 6 окремих `count`-запитів, підказка фільтра, рендер трьох різних відповідей. Кожен полл платить за всі шість. — Винести у `services/`, лічильники рахувати лише для повного рендера.

## LOW

- `app/mail_reader.py:581` — `date.today()` для вікна IMAP; файл не входить у список сторожа `tests/test_business_day.py:99-112`. Тут нешкідливо (пошукове вікно), але дірка сторожа реальна. — Додати `app/mail_reader.py` і `app/routers/mail.py` у список свідомо.
- `app/models.py:573` — `saved_path` як `String(500)`: UNC + `export/<клієнт>/<дата>/<матеріал>/<довге імʼя STL>` підбирається до межі. SQLite не обрізає, Postgres обріже. — `Text`.
- `app/mail_reader.py:653-742` — обрив IMAP у фазі 2 не перериває цикл: кожен із решти `pending` рядків окремо ловить виняток і логує трасу (до 250 разів), поки не спрацює вотчдог. — Розрізняти «помилка листа» і «зʼєднання вмерло».
- `app/archive_extract.py:206-260` — вкладений архів (zip у zip) розпаковується на один рівень і лишається файлом; `extract_archive_attachments` його вже не бачить. — Документувати або повторний прохід.
- `app/link_attachments.py:35, 83` — `_ALLOWED_SUFFIXES` дозволяє БУДЬ-ЯКИЙ `*.ukr.net` на редиректі, тоді як екстрактор випускає лише `dl|edisk`. Ширше, ніж потрібно. — Звузити.
- `app/mail_spool.py:110-112` — тека нульового розміру потрапляє у `prunable_dirs` без внеску в `prunable_bytes` (правильно), але `total_dirs` в UI-тексті читається як «є що чистити». Косметика звіту.
- `app/mail_parser.py:18` — `kind_guess` ловить `фрезеруванн\w*` будь-де і забирає решту рядка у «вид роботи». `(прогін)`: «на фрезерування циркон» → kind='циркон'. — Вимагати двокрапку або початок рядка.

## Verified OK

- `archive_extract.py` — flatten до basename (zip-slip неможливий), ліміти 2000 записів / 2 ГБ з перевіркою РЕАЛЬНО прочитаних байтів, повне прибирання за собою, зрозуміла помилка при відсутньому UnRAR плюс пошук WinRAR/7z поза PATH.
- `mail_export.sanitize_folder_name` / `_contained_child` — `(прогін)`: `..`, `../evil`, `..\evil`, `CON`, `a/b` знешкоджені; вихід за корінь відхиляється.
- `link_attachments._get_checked` — ручний прохід редиректів із перевіркою хоста ПЕРЕД кожним запитом (справжній захист від SSRF через open redirect), ліміт хопів, потоковий ліміт розміру, роздільний `(connect, read)` таймаут.
- `mail_export._move_file` / `undo_moves` + `mail_accept` — порядок «файли → база → таблиця», компенсація переміщень при провалі коміту, помилки відкату повідомляються, не ковтаються.
- Жоден роут у `routers/mail.py` не `async def` — записи в Google-таблицю (`append_mail_placeholder_row`, `clear_order_row`) не потрапляють на event loop; `clear_order_row` звіряє позицію рядка (CLAUDE.md §14, «Запис у таблицю»).
- `mail_reader._unlink_after_commit` — пара `after_commit`/`after_rollback` з одноразовим прапорцем: архів не зникає, поки коміт не пройшов, і не зноситься на коміті НАСТУПНОГО листа.
- Тести підмінюють символи в ОБОХ модулях (`mail_router_mod` + `mail_accept_svc`), тож переїзд логіки в сервіс не зробив підміни тихими no-op; автоперевірка всіх `monkeypatch.setattr` у поштових тестах не знайшла жодного відсутнього атрибута.
- `mail_filters._bump_hits` — атомарний SQL; `apply_filters_to_email` не перештамповує вже розштампований лист.
- `_files_changed_response` — `json.dumps` з `ensure_ascii=True` (заголовок latin-1); `/mail/senders/*` не конфліктує з `/mail/{email_id}`; усі 22 адреси з шаблонів мають роут; стан візарда живе в hidden-полях POST, а не в query string.
- `mail_reader._file_is_missing` — правильна асиметрія: втрата лише на чистому `FileNotFoundError`, будь-яка інша `OSError` = «шара мовчить».

## Architecture notes

1. Три різні уявлення про «файл є»: `Path.exists()` (accept, панель, triage_status), `_file_is_missing` (redownload) і `order_id is not None` (`files_on_disk`). Мають бути одним предикатом — це найкоротший шлях до втрати файлу.
2. `(uid, uid_validity)` — ключ у базі, але не в іменах тек спула і не у двох ручних фетчах. Namespace має бути наскрізним, інакше гарантія моделі не діє на диску.
3. Диск читається щонайменше пʼять разів за рендер списку тріажу і повторюється кожні 15 с. Потрібен один прохід «стан файлів листа» на запит, як `_tech_root_children` для тек техніків.
4. `routers/mail.py` наполовину розвантажений (`services/mail_accept.py`), але `get_mail`, `_unaccept_email` і весь блок фільтрів досі доменна логіка в HTTP-шарі — саме там і сидять два CRITICAL.
5. Нечітке зіставлення клієнта живе у двох напрямках (видача і прийняття) на одній функції, але прийняття викликає її з `known_aliases={}` — накопичувальний словник підтверджених пар (CLAUDE.md §4) на цьому боці не працює.
6. `staged_to_export` і `auto_accept` — дві напівреалізовані фічі, що читаються як робочі. Мертвий код у шляху «файл не має загубитись» дорожчий за звичайний.
