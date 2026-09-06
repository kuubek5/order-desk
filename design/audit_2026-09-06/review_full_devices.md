# Ревʼю області «пристрої» — 07.09.26

Печі (VNC/OCR) / верстати (агент, RemiCORE OCR, SISMA) / STL-превʼю / зображення зміни / Go-агент.
Зібрано з чотирьох суб-ревʼю. VERIFIED = прочитано код; «виправлено» — уже в master (`dedecfc`).

## CRITICAL
- `app/machine_sisma.py:127` — `load_sisma_glyphs` під `@lru_cache(maxsize=1)`: один транзієнтний збій читання (файлу ще нема, антивірус тримає) закріплював `{}` до кінця життя процесу → SISMA «раптом» переставала читатись до рестарту. `machine_ocr._cache_only_success` написаний саме проти цього. **Виправлено** (той самий декоратор).

## HIGH
- `app/services/machines.py:1303-1313` — `strip_summary` рахував SISMA у «фрезерує / без звʼязку», хоча `machine_side_context` принтер зі списку прибирає. **Виправлено** (фільтр `is_sisma_machine`).
- `agent/main.go:96,573` — агент слухає `0.0.0.0` без TLS, токен у cleartext; firewall-правило (`openFirewall`, :923-931) відкрите з БУДЬ-ЯКОГО IP, не лише з CRM. Мінімум — обмежити правило IP-адресою ПК CRM; повний фікс — TLS (self-signed, pinned у CRM). **PLAN D.1 ⚠**
- `app/services/machines.py:666-868` — `poll_target` ~200 рядків.

## MEDIUM
- `app/services/furnace.py:397-436` — поля `FurnaceState` пишуться поза `_states_lock`; фоновий тік і ручне «Оновити зараз» паралельно → torn state (`reading`/`error` з різних проходів). `machines.py:795-825` робить правильно (збирає, потім присвоює під локом). **PLAN D.2**
- `app/furnace_ocr.py:397-435` — «голосування трьох сигналів» насправді два (`word`, `button`); `step` лише попередження; якщо один сигнал None — другий вирішує сам. Документувати або вимагати ≥2 збіги.
- `app/furnace_ocr.py:110,349-378` — патерн зони `command` дозволяє `.` будь-де — єдина зона без структурної гарантії «хибне число гірше за жодне».
- `app/furnace_vnc.py:66-84` — `asyncio.wait_for` cancel: немає доказу закриття VNC-сокета при зависанні всередині `asyncvnc.connect` (тест лише на refused/success). Можливий витік за добу з мертвою піччю.
- `app/shift_images.py:136-151` — без капу пікселів ДО декодування (PNG 10 МБ → сотні МБ RAM). **Виправлено** (`MAX_IMAGE_PIXELS` = 40 Мп, перевірка `probe.size`).
- `app/shift_images.py:148-151` — EXIF-орієнтація губилась (фото табло боком). **Виправлено** (`ImageOps.exif_transpose`).
- `app/static/js/stl-preview.js:99-100,441,562` — `geometryCache`/`fileListCache` без ліміту — heap ріс усю зміну. **Виправлено** (LRU 40/200 із `dispose()`).
- `agent/main.go:560-563, 267-279` — токен від 8 символів без rate-limit; `/info` віддає токен plaintext без auth на loopback (:513-538); `agent.json` 0644 і `crm-setup.txt` з токеном у Program Files, не видаляються при деінсталяції; `randomToken` fallback «changeme-»+UnixNano. **PLAN D.1**
- `app/machine_sisma.py:153-221` — власний пайплайн mask→lines→glyphs, паралельний до `furnace_ocr._bitmap/_segments/_match_digit`, який `machine_ocr` свідомо перевикористовує.
- `scripts/machine_collect_frames.py:59-66` — `response.content` без капу/дедлайну (бойовий `_capture_http` має `MAX_FRAME_BYTES` + дедлайн).

## LOW
- `app/services/machines.py:549-558` — `_HOST_RE` у `resolve_frame` — мертва перевірка (діє лише при `not known`). `frame_path` без `_sanitize_key` (безпечно, бо key з `validate_address`).
- `app/services/furnace.py:244-247` — `config_error` завжди `None` (вестигіальне). Framebuffer VNC без капу розміру.
- Мертвий код: `MachineConfigError` (ніде не raise/except; споживачі ловлять `FurnaceConfigError`); `SismaReading.printing` — лише тести й dev-скрипт.
- `app/stl_preview.py` / `app/shift_images.py` / `app/order_folder.py` — три копії path-safety (`_is_link` дослівно) — свідомо, але виправлення обходу треба буде робити тричі.
- STL-токени без терміну; TOCTOU на ліміті 4 картинок у нотатці; `agent/main.go:269-272` `?token=` як fallback (сервер шле лише заголовок).

## Verified OK
- Печі: `_MAX_MISMATCH_PIXELS = 0` (pixel-exact або None), одна сесія БД на потік (`poll_all` паралелить лише мережу), atomic `os.replace` кадру, `FurnaceReading` prune 30 дн., read-only VNC (`shared=1`, 0 input-подій, зʼєднання закриваються), порядок роутів літерал → `{key}`.
- Верстати: `ThreadPoolExecutor(min(16,n))`, `AGENT_TIMEOUT`/`AGENT_TOTAL_DEADLINE`, `MAX_FRAME_BYTES` 24 МБ під час стрімінгу, капи `MAX_OUTAGES`/`HISTORY_MAX_SPANS`/`CALIBRATION_MAX_FRAMES` з евікшеном за новизною; OCR all-or-nothing (відсоток лише з підтвердженням підпису, `layer > layers_total` → None); SISMA `Enabled` ≠ простій; порядок роутів + сторож.
- Агент: серверна auth лише заголовком `X-Agent-Token`, `ConstantTimeCompare`, токен не логується, без self-update, без shell injection (argv-форма), settings-сторінка лише `127.0.0.1:8766`, `/save` вимагає JSON (CORS preflight ⇒ CSRF практично закритий).
- STL/зображення: traversal закрито (root з серверного резолвера, symlink-перевірка на кожному сегменті, `resolve(strict)`+`relative_to`), XSS нема (`textContent`), WebGL dispose у галереї, `.part` завжди прибирається, SVG виключено, `media_type_for` за whitelist розширень.
