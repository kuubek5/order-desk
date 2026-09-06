# KuubMill — рев'ю безпеки та архітектури (read-only)

Дата: 05.09.26 · Гілка `master` · Скоуп: автентифікація, сесії, секрети, авторизація роутів, шляхи/процеси, віддача файлів.

**Загрозова модель, під яку оцінювалась серйозність:** одна Windows-машина, слухач лише на `127.0.0.1:8000`, двоє довірених операторів + адмін, локальна мережа лабораторії (Synology export, мережеві шари), без інтернет-експозиції. Тому «теоретичний мережевий зловмисник» тут не рахується як реальний вектор — але все, що дає **не-адміну можливості адміна** або **ламає намір самого автора**, позначено чесно.

**Підсумок:** critical — немає. Одна high (Google OAuth). Решта — medium/low, переважно «захист тримається на одній ниточці, і ця ниточка ніде не зафіксована тестом».

---

## 1. Знахідки

### HIGH

#### H-1. Google OAuth: немає `state` і немає PKCE — ін'єкція коду авторизації
`app/google_oauth.py:96` (сервер), `app/google_oauth.py:118-127` (URL згоди), `app/google_oauth.py:139-152` (обмін коду)

Loopback-флоу побудований без обох обов'язкових для native-app елементів RFC 8252 (§8.9 `state`, §8.1 PKCE):

```python
auth_url = config.auth_uri + "?" + urlencode({
    "client_id": ..., "redirect_uri": redirect_uri, "response_type": "code",
    "scope": ..., "access_type": "offline", "prompt": "consent",
})          # ← ні state, ні code_challenge
...
server.handle_request()          # приймає ПЕРШИЙ-ЛІПШИЙ GET на ефемерний порт
params = server.query_params or {}
...
codes = params.get("code")       # ← жодної перевірки, що код «наш»
```

**Сценарій.** Поки триває 180-секундне вікно, будь-яка сторінка, відкрита в браузері адміна (реклама на будь-якому сайті, банер), може перебрати ефемерні порти `127.0.0.1:1024-65535` і смикнути `http://127.0.0.1:PORT/?code=<код зловмисника>`. `handle_request()` обробляє рівно один запит — тобто чужий запит:
1. **З'їдає** справжній редірект від Google → флоу падає в «тайм-аут» без пояснення (це вже реальний баг стабільності, не тільки безпеки);
2. Або, якщо влучив першим із валідним кодом, KuubMill обміняє **чужий** код і збереже **чужий** refresh token у `google_oauth_refresh_token`. Далі синк таблиці ходить у Google під акаунтом зловмисника: у кращому разі синк мовчки мертвий, у гіршому — записи черги течуть у чужу таблицю, якщо ID збігся.

**Фікс (мінімальний):**
```python
state = secrets.token_urlsafe(32)
auth_url = ... urlencode({..., "state": state, "code_challenge": challenge,
                          "code_challenge_method": "S256"})
# у циклі: while не отримали запит із нашим state — handle_request() ще раз
if (params.get("state") or [""])[0] != state:
    raise OAuthFlowError("Невідповідність state — авторизацію відхилено")
# і code_verifier у POST обміну
```
`handle_request()` варто загорнути в цикл із дедлайном, щоб сторонній GET не вбивав флоу.

---

### MEDIUM

#### M-1. `POST /settings/check-path` — довільна тека на машині/шарі, доступно НЕ-адміну
`app/routers/settings.py:535-569` (роут), `app/routers/settings.py:176-224` (`check_path_status`)

Гейт: `get_current_user` + `is_loopback_request`. **Адміна немає свідомо** (коментар :553-555). Шлях приходить сирим із форми і не обмежений жодним коренем:

```python
marker = path / f".orderdesk-check-{uuid.uuid4().hex}.tmp"   # :210
marker.write_bytes(b"")                                       # :212
...
marker.unlink(missing_ok=True)                                # :220
```

**Сценарій.** Оператор (не адмін) шле `export_folder_path=\\synology\admin$\...` або `C:\Users\<адмін>\AppData\...` і отримує оракул «існує / це тека / є права на запис» по всій машині й по всіх мережевих шарах, до яких дотягується акаунт служби KuubMill — а це не той акаунт, під яким оператор сидить у Windows. Плюс створення й видалення файлу в будь-якій такій теці.

**Фікс.** Або додати `require_settings_admin` (шляхи все одно налаштовує адмін), або лишити операторові тільки read-probe (`exists()`/`is_dir()`) без запису маркера, а write-probe перевести під адміна.

#### M-2. ПІН «Виробітку» ламається перебором за секунди
`app/routers/vyrobitok.py:116-118`

```python
expected = get_setting(db, "vyrobitok_pin")
if expected and pin.strip() == expected.strip():
    request.session[_PIN_SESSION_KEY] = time.time() + _PIN_TTL_SECONDS
```

Порівняння не константного часу, і — головне — **жодного обмеження спроб**. Розділ захищає зарплатні цифри від самих операторів, тобто це єдиний внутрішній контроль конфіденційності в застосунку, і 4-значний ПІН перебирається скриптом з-під звичайної сесії за кілька секунд.

**Фікс.** `secrets.compare_digest(pin.strip(), expected.strip())` + лічильник невдач у сесії з експоненційною затримкою (5 спроб → блок на 5 хв). Дешево і достатньо для цієї моделі.

#### M-3. Адмін створює операторів без вимог до пароля і з довільною роллю
`app/routers/settings.py:2012-2049` (`create_operator`), `app/routers/settings.py:2103-2124` (`reset_operator_password`)

```python
role = form.get("role", "оператор").strip() or "оператор"   # :2018 — БЕЗ whitelist
...
if not username or not password:                             # :2029 — єдина перевірка
```

Два наслідки:
- **Пароль.** `validate_first_admin` (`app/services/operators.py:36`) вимагає 10 символів, `/account/password` — 6, а адмінське створення й скидання — **жодного мінімуму**. Оператор із паролем `1` цілком проходить. Три різні політики в трьох місцях = політики нема.
- **Роль.** Рядок вільний. Одруківка «aдмін» з латинською `a` створює акаунт, що в списку виглядає адміном, а всі гейти (`user.role != "адмін"`) його не пускають — діагностувати це важко. Симетрично: `section_gate.non_admin_roles()` (`app/services/section_gate.py`) будує таргетинг блокаторів із реальних значень `User.role`, тож сміттєва роль осідає в UI назавжди.

**Фікс.** Спільна `validate_password(password)` (одна межа для всіх трьох точок) + `if role not in {"адмін", "оператор"}: 400`.

#### M-4. CSRF не існує як механізм — усе тримається на `same_site="strict"`
`app/web.py:732-738`; жодного токена в `app/**/*.py` і `app/templates/**` (grep за `csrf` — нуль влучань)

```python
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY,
                   same_site="strict", https_only=False, max_age=8*60*60)
```

Зараз це **не експлуатується**: `SameSite=Strict` обрізає крос-сайтові POST-и, а всі мутації — POST. Але: (а) захист повністю невидимий у коді (жоден коментар не каже «це наш CSRF»), (б) **немає тесту-сторожа** на `same_site`, і одне майбутнє послаблення до `lax` мовчки відкриває всі HTMX-мутації (`/settings/users`, `/vyrobitok/pin`, дії над роботами) для будь-якої сторінки в браузері оператора, (в) `https_only=False` разом із `Dockerfile:29` (`--host 0.0.0.0`) означає, що будь-який запуск через Docker у мережі лабораторії віддає куку сесії відкритим HTTP.

**Фікс (порядок за ціною).** 1) Тест у `tests/test_route_inventory.py`-стилі, що фіксує `same_site == "strict"` і `https_only` — 5 рядків, ловить регрес. 2) Перевірка заголовка `Origin`/`Sec-Fetch-Site` в одному middleware для всіх не-GET (HTMX його шле). 3) Токен — тільки якщо застосунок колись вийде за loopback.

#### M-5. `/docs`, `/openapi.json`, `/redoc` відкриті без сесії
`app/web.py:728-731` — `FastAPI(...)` без `docs_url=None, redoc_url=None, openapi_url=None`

`license_gate` (`app/web.py:821`) їх закриває **лише поки ліцензії немає**. З валідною ліцензією `GET /openapi.json` віддає повну карту роутів, імена полів форм і схеми будь-кому, хто дотягнувся до порту, — без входу. Самі роути далі гейтяться, тож це розвідка, не доступ; але вона безкоштовно розкриває поверхню (`/settings/users/{id}/reset-password`, `/vyrobitok/pin`, `/diag/*`).

**Фікс.** `FastAPI(title="KuubMill", docs_url=None, redoc_url=None, openapi_url=None, ...)` — застосунок не має API-клієнтів, схема нікому не потрібна. Або додати шляхи в гейт сесії.

#### M-6. Немає обмеження спроб входу
`app/routers/auth.py:161-179`

`POST /login` без лічильника невдач, без затримки, без блокування акаунта. Пом'якшено тим, що слухач лише на loopback (з мережі не дістати) і bcrypt повільний (~100 мс/спроба ≈ 10 спроб/с). Реальний сценарій — фізичний доступ до ПК, коли оператор відійшов; тоді перебір слабкого пароля (див. M-3, де мінімуму немає взагалі) цілком проходить.

**Фікс.** Лічильник у пам'яті процесу по `(username, ip)`: після 5 невдач — 30 с паузи, після 10 — 5 хв. Без БД, ~20 рядків.

---

### LOW

#### L-1. `POST /diag/perf/client` обходить `get_current_user`
`app/routers/diag.py:156` — `if request.session.get("user_id") is None: 401`

Деактивований оператор із живою кукою проходить (усі інші роути через `get_current_user` його відсікають і чистять сесію). Пише лише в кільцевий буфер у пам'яті. Фікс — той самий `get_current_user`.

#### L-2. `/diag/*` доступні адміну по мережі, без loopback-конверта
`app/routers/diag.py:35-42` (`_require_admin` не має `is_loopback_request`, на відміну від `require_settings_admin` у `app/routers/settings.py:252`)

`/diag/perf` і `/diag/perf.txt` віддають шляхи, **query-стрінги** і статуси всіх недавніх запитів. Query-стрінги можуть містити імена клієнтів/фільтри. Незначно, але асиметрія з settings ненавмисна.

#### L-3. Асиметрія loopback-гейта в адмінських роутах
`app/routers/settings.py:2012, 2053, 2079, 2103` (users/*), `:640` (`/settings/imap`), `:675` (`/settings/test-imap`) — адмін **без** loopback; водночас `furnaces/password`, `machines/password`, `backup/*`, `update/*` — адмін **+** loopback.

Тобто адмін по мережі може створити оператора або переписати IMAP-креденшли, але не може змінити пароль печі. Виглядає як історичний дрейф, а не рішення. Або підвести все під `require_settings_admin`, або свідомо задокументувати, чому users/* м'якші.

#### L-4. `/settings/backup/import` — файл цілком у пам'ять, без ліміту
`app/routers/settings.py:1755` — `raw = await backup_file.read()`

Адмін + loopback, тож це самообстріл, не атака. Але 2-гігабайтний файл кладе процес. Фікс — перевірка `content-length` / потокове читання з межею.

Сам формат бекапу зроблено **правильно** (`app/backup.py:149-201`): JSON-конверт, PBKDF2-HMAC 480 000 ітерацій, Fernet із власною автентифікацією, `envelope["app"] != "order-desk"` → `BackupFormatError`. Живу `.db` не підміняє — вставляє через ORM. Єдине зауваження: мінімум пароля 8 символів (`settings.py:1709`) для файлу, що містить **усі секрети + усі хеші паролів**, — я б підняв до 12.

#### L-5. Немає абсолютного строку життя сесії
`app/web.py:737` — `max_age=8*60*60`

Starlette перевидає куку на кожній відповіді, тому 8 годин — це **ковзний** тайм-аут неактивності, а не стеля. Оператор, що тримає вкладку відкритою, не перелогінюється ніколи. Для цеху це, найпевніше, і є бажана поведінка — фіксую, щоб було свідомим рішенням, а не побічним ефектом.

#### L-6. `SESSION_SECRET_KEY` детерміновано виводиться з `DB_ENCRYPTION_KEY`
`app/config.py` (останні рядки) — `sha256("order-desk-session:" + DB_ENCRYPTION_KEY)`

Витік ключа шифрування дає ще й підробку сесій. На практиці витік цього ключа й так означає доступ до всіх секретів, тож приросту ризику майже нема. Приємний побічний ефект: зміна ключа інвалідує всі сесії.

#### L-7. `Dockerfile:29` слухає `0.0.0.0`
Разом із `https_only=False`, відсутністю rate-limit і відкритим `/openapi.json` це означає, що випадковий запуск dev-образу в мережі лабораторії віддає застосунок цілком. CLAUDE.md §6 каже «Docker — додатково для dev», тож це попередження, а не діра. Варто хоча б коментар у Dockerfile.

#### L-8. Токен `stl-preview` прозорий і підробний за задумом
`app/stl_preview.py:181-183` — `base64url("<root_key>:<relative/path>")`, без підпису

Наслідок: **будь-який** залогінений оператор може перебирати токени й читати `.stl` з будь-якої підтеки `export`/`mail`/`tech`. Це збігається з пласкою моделлю довіри лабораторії (двоє операторів бачать усю чергу), і сама межа кореня тримається залізно (див. §3). Фіксую як усвідомлену межу, не як баг.

#### L-9. `bcrypt==4.0.1` — пін заради сумісності з passlib
`requirements.txt:14-20`

Пін обґрунтований коментарем (passlib читає `bcrypt.__about__`). Наслідки: (а) не приймаються оновлення безпеки bcrypt, (б) bcrypt мовчки обрізає пароль на 72 байтах. Обидва — прийнятні тут, але варто мати в полі зору перехід на `pwdlib`/`argon2` при наступному апгрейді passlib.

#### L-10. `/health` до входу й до ліцензії
`app/web.py:905-913` — віддає `{"status": "ok", "version": VERSION}`

Версія свідомо не секрет (потрібна smoke-тесту релізу й watchdog'у оновлення). Погоджуюсь із рішенням; фіксую лише те, що це єдиний pre-auth роут, який щось розкриває.

---

## 2. Таблиця авторизації (роути з рев'ю)

Скорочення: `сесія` = `get_current_user` + 401/редірект · `RSA` = `require_settings_admin` (адмін **+** loopback) · `LB` = `is_loopback_request`

| Роут | file:line | Вхід | Адмін | LB |
|---|---|---|---|---|
| `GET /settings` | settings.py:265 | сесія | **немає** (секрети маскуються, картки ховає шаблон) | — |
| `POST /settings` | settings.py:457 | сесія | частково (`OPERATOR_EDITABLE_KEYS`, :472) | — |
| `POST /settings/check-path` | settings.py:535 | сесія | **немає** (свідомо) | ✅ |
| `POST /settings/users` | settings.py:2012 | сесія | ✅ | ❌ |
| `POST /settings/users/{id}/initial` | settings.py:2053 | сесія | ✅ | ❌ |
| `POST /settings/users/{id}/toggle-active` | settings.py:2079 | сесія | ✅ | ❌ |
| `POST /settings/users/{id}/reset-password` | settings.py:2103 | сесія | ✅ | ❌ |
| `POST /settings/backup/export` | settings.py:1688 | сесія | ✅ | ✅ |
| `POST /settings/backup/import` | settings.py:1727 | сесія | ✅ | ✅ |
| `POST /settings/furnaces/password` | settings.py:1071 | RSA | ✅ | ✅ |
| `POST /settings/machines/password` | settings.py:1226 | RSA | ✅ | ✅ |
| `POST /settings/imap`, `/test-imap` | settings.py:640, 675 | сесія | ✅ | ❌ |
| `POST /settings/update/*` | settings.py:1925-1969 | сесія | ✅ | ✅ |
| `GET /diag/perf`, `/diag/perf.txt` | diag.py:84, 104 | сесія | ✅ | ❌ |
| `POST /diag/perf/clear` | diag.py:138 | сесія | ✅ | ❌ |
| `POST /diag/perf/client` | diag.py:147 | **сирий `session["user_id"]`** (L-1) | немає (свідомо) | ❌ |
| `POST /open-folder` | stl.py:69 | сесія | немає | ✅ |
| `POST /mail/{id}/open-folder` | mail.py:774 | сесія | немає | ✅ |
| `GET /stl-preview/{token}` | stl.py:25 | сесія | немає | — |
| `GET /stl-preview/{token}/{file}` | stl.py:44 | сесія | немає | — |
| `GET /feedback/images/{id}` | feedback.py:187 | сесія | немає | — |
| `GET /shift/images/{id}` | shift.py:213 | сесія | немає | — |
| `POST /feedback/{id}/seen|resolve|reopen` | feedback.py:145-173 | `_require_admin` | ✅ | ❌ |
| `POST /license` | auth.py:69 | **немає** (за задумом, гейт ліцензії) | — | — |
| `GET/POST /setup` | auth.py:100, 110 | немає, але `user_count(db) != 0` → `/login` | — | — |
| `/docs`, `/openapi.json`, `/redoc` | web.py:728 | **немає** (M-5) | — | — |

Глобальних admin-`dependencies` на рівні `APIRouter` чи `include_router` немає ніде — усі 17 роутерів створюються порожнім `APIRouter()`. Єдина глобальна залежність — `Depends(_mark_route_entry)` (`web.py:730`), це вимірювання перфу.

---

## 3. Що зроблено добре

1. **Робота зі шляхами — зразкова.** `app/stl_preview.py:268-311` і `app/order_folder.py:150-232`: корінь **завжди** підставляє сервер із налаштувань, клієнт впливає лише на відносний хвіст; відкидаються `..`, `.`, абсолютні шляхи, диск-літера, UNC; обхід посегментний із відмовою на будь-якому symlink/junction; фінальний `resolve(strict=True)` + `relative_to(resolved_root)` доводить фактичну належність кореню **після** резолву лінків. Path traversal не знайдено ніде.
2. **Жодного `shell=True`, `os.system`, `os.startfile`.** Єдиний запуск Провідника — `subprocess.Popen(["explorer", str(folder)])` списком аргументів (`app/platform_windows.py:54`), і шлях туди приходить уже провалідованим резолвером, ніколи сирим із форми. Роут оновлення (`app/update_check.py:475`) теж передає лише серверні константи.
3. **Секрети не течуть у шаблони.** `SECRET_SETTING_KEYS` (`app/settings_store.py:144`) вирізаються з Jinja-контексту в `GET /settings` (`settings.py:288-290`) — у шаблон іде лише `bool`. Мотивація («сторінка помилки Jinja з дампом контексту віддала б пароль пошти») записана прямо в коді. Grep по логах не дав жодного місця, де секрет пишеться в лог.
4. **`InvalidToken` деградує, а не 500-ить.** `get_setting` (`settings_store.py:224`) при зміні `master.key` повертає `None` і пише попередження без значення, а `setting_unreadable` (`:237`) дозволяє екрану ліцензії відрізнити «не активовано» від «ключ змінився» (`app/license.py:160-175`). Один із найкраще оброблених крайніх випадків у проєкті.
5. **DPAPI-ініціалізація без гонок.** `load_or_create_master_key` (`app/windows_dpapi.py:130-170`): відкриття `"xb"` (ексклюзивне створення), `fsync`, при `FileExistsError` — перечитати чужий ключ, ніколи не перезаписувати нечитабельний блоб. Плюс `load_dotenv(override=False)` у `config.py`, щоб залежалий plaintext не перебив DPAPI.
6. **Ліцензія — офлайн Ed25519 з прив'язкою до `machine_id`, і `verify_license_key` не кидає ніколи** (`app/license.py:102-155`): кожна гілка повертає `LicenseStatus`, тобто пошкоджений ключ не може покласти застосунок.
7. **Завантаження зображень обмежене нормально** (`app/feedback_images.py:57-118`, `app/shift_images.py:106-163`): потокове читання з обривом на 10 МБ, максимум 4 файли, перекодування через Pillow (вбиває поліглот-файли), віддача з `X-Content-Type-Options: nosniff` і whitelist розширень. Обидві теки свідомо **не** в `/static` — з поясненням у `app/config.py`.
8. **Тости не дають XSS**: `app/static/js/app.js:405-406` кладе динамічний текст через `textContent`, а не в `innerHTML`. `|safe` у шаблонах трапляється тричі й лише на серверних константах (`section_gate.VARIANTS`, іконки, готовий фрагмент), не на введенні користувача.
9. **Бекап зроблений як треба**: PBKDF2-HMAC 480k, Fernet, salt, перевірка конверта, вставка через ORM (`app/backup.py`).

---

## 4. Архітектурні зауваження

### A-1. Чотири паралельні реалізації «це для адміна»
- `require_settings_admin` — `app/routers/settings.py:252` (адмін + loopback, кидає 401/403)
- `_require_admin` — `app/routers/diag.py:35` (редірект + 403, **без** loopback)
- `_require_admin` — `app/routers/feedback.py:40` (401 + 403, **без** loopback)
- ~17 інлайнових `if user.role != "адмін": raise HTTPException(403)` у `settings.py`

Розбіжність не косметична — саме вона й породила L-3 (users/* і imap без loopback) та L-2. Гейт авторизації — це доменне рішення, і за правилом CLAUDE.md §14 («будує `Response` → routers; ні → services») йому місце як **одній** залежності в `app/routers/deps.py`:

```python
def require_admin(*, loopback: bool = True): ...   # FastAPI Depends
```
з явним `loopback=False` там, де м'якість свідома. Тоді таблиця з §2 стає похідною від коду, а не від дисципліни. Сторож на кшталт `tests/test_route_inventory.py` міг би фіксувати «множина адмін-роутів», щоб новий роут не з'явився без гейта.

### A-2. `app/routers/settings.py` — 2280 рядків, 53 роути
Це найбільший файл шару і, судячи з §2, найнеоднорідніший за політикою доступу. Природні шви вже видно: печі/верстати (13 роутів), знімки таблиці (6), оновлення (3), users (4), матеріали (5). Розбиття на `routers/settings/{credentials,devices,users,sheets,update}.py` зробило б аудит доступу оглядовим — зараз щоб відповісти «чи цей роут адмінський», треба читати тіло функції.

### A-3. `app/routers/deps.py` (482 рядки) переріс роль «спільної бази»
Зараз він тримає: сесію БД, поточного оператора, `Jinja2Templates` із вимірюванням, ~20 Jinja-глобалів, чотири глобали, що **самі відкривають сесію БД на кожному рендері** (`notify_prefs`, `shift_pending`, `feedback_open_count`, `ui_prefs`), тости й `static_ver`. Кожен із цих глобалів обґрунтований окремо й переконливо, але разом це «кожен рендер = 4+ підключення до SQLite і кілька Fernet-розшифрувань» — саме те, що `_timed_global` і був змушений почати міряти. Реєстрацію Jinja-глобалів варто винести в `app/templating.py`, лишивши в `deps.py` тільки залежності запиту.

### A-4. Три різні політики пароля
`validate_first_admin` — 10 символів (`app/services/operators.py:36`), `/account/password` — 6 (`app/routers/auth.py:349`), адмінське створення/скидання — жодного (`settings.py:2029, 2116`). Політика має бути однією функцією в `app/services/operators.py`, яку кличуть усі три точки (див. M-3).

### A-5. Testability: `is_loopback_request` і роль важко тестувати разом
Гейти читають `request.client.host` і `user.role` всередині тіл роутів, тож перевірка «цей роут закритий» вимагає підняти HTTP-клієнт. З `Depends(require_admin(...))` (A-1) з'являється можливість статичного сторожа: пройтись `app.routes` і звірити, що кожен шлях під `/settings` і `/diag` має цю залежність. Це саме той тип тесту, який CLAUDE.md §14 вже цінує (два наявні сторожі).

### A-6. Дубль у `app/config.py`
`MACHINE_PORTRAITS_PATH` оголошено **чотири рази** поспіль з ідентичним коментарем (наслідок мерджа). Не баг — але в файлі, де поруч живе `DB_ENCRYPTION_KEY`, зайвий шум небажаний.

---

## 5. Пріоритет виправлень

| # | Що | Ціна |
|---|---|---|
| 1 | H-1: `state` + PKCE + цикл `handle_request` у Google OAuth | ~30 рядків |
| 2 | M-3: одна `validate_password` на три точки + whitelist ролей | ~15 рядків |
| 3 | M-2: `compare_digest` + лічильник спроб ПІНа | ~15 рядків |
| 4 | M-5: `docs_url=None, redoc_url=None, openapi_url=None` | 1 рядок |
| 5 | M-1: адмін-гейт (або зняття write-probe) на `check-path` | 2 рядки |
| 6 | M-4: тест-сторож на `same_site="strict"` | ~10 рядків |
| 7 | M-6: лічильник невдалих входів у пам'яті | ~20 рядків |
| 8 | A-1: єдиний `require_admin` у `deps.py` + перевід L-2/L-3 | рефактор, окремий блок |
