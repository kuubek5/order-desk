"""Спільна база HTTP-шару: сесія БД, поточний оператор, шаблони, тости.

Кожен роутер імпортує звідси, а не з `app.web` — інакше вийшло б кільце
(web підключає роутери, роутери тягли б web назад). Тому цей модуль НЕ
імпортує ні `app.web`, ні жоден роутер.
"""

import ipaddress
import json
import logging
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import perf
from app.business_day import business_today, utc_now, utc_to_business
from app.__version__ import VERSION
from app.db import SessionLocal
from app.license import LICENSE_EXPIRY_WARNING_DAYS, get_license_status
from app.material_class import (
    material_badge,
    material_families,
    material_family_class,
    material_color_css_class,
    split_material_color,
    strip_material_word,
)
from app.models import ActionLog, User
from app.runtime import resource_path
from app.settings_store import (
    DEFAULT_NOTIFY_POSITION,
    DEFAULT_NOTIFY_STYLE,
    NOTIFY_EVENTS,
    get_notify_events,
    get_notify_position,
    get_notify_style,
)
from app.services.queue import is_rush_comment
from app.services.settings_nav import can_edit, can_see, nav_payload, visible_nav
from app.services.shift import night_label, open_note_count
from app.statuses import STATUSES, is_overdue, status_dot
from app.sync_control import SYNC_SPEED_PRESETS, get_sync_speed
from app.triage_status import files_on_disk, triage_readiness
from app.update_check import get_known_update

logger = logging.getLogger(__name__)


# Shown when a table-writing action is attempted while sync is paused. The
# action is refused and NOTHING changes — not even the DB — so there is no
# divergence for the resume read to revert. The operator retries after resume.
SYNC_PAUSED_MSG = (
    "Синхронізацію таблиці призупинено — зміну не збережено. "
    "Зніміть паузу, щоб продовжити."
)


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    # Покоління сесій: зміна пароля збільшує лічильник, і всі раніше видані
    # сесії стають недійсними. Сесії, видані ДО появи поля, покоління не
    # несуть — вважаємо їх нульовими, щоб оновлення не викинуло зміну з
    # системи посеред дня (ревʼю 07.09.26, K.8).
    if int(request.session.get("epoch", 0)) != int(getattr(user, "session_epoch", 0) or 0):
        request.session.clear()
        return None
    return user


def login_redirect(request: Request) -> Response:
    """Відповідь на «немає сесії», що не ламається під HTMX.

    Проста навігація має отримати 303 → /login, як і раніше. Але коли запит
    прийшов від HTMX (полл рядків черги, свап панелі), браузерний XHR сам
    ходить по 303 і тягне ПОВНУ сторінку входу з кодом 200 — а htmx свапає
    її в слот фрагмента, без її <head> і login.css. Оператор бачив голу
    форму всередині мертвої рейки (лог: GET /?…&partial=rows → 303 → GET
    /login 200, поряд /furnaces/side і /machines/side віддавали 401).

    Для HTMX віддаємо 204 + HX-Redirect: браузер робить СПРАВЖНЮ навігацію
    на /login з її власним <head>. Той самий прийом уже стоїть у mail.py
    (HX-Redirect drives a real navigation) — тут він стає спільним для всіх
    guard-сайтів «user is None».
    """
    # getattr, не request.headers напряму: фейкові request'и в тестах
    # (types.SimpleNamespace) заголовків не мають — той самий контракт, що в
    # ui_prefs із request.state. За відсутності заголовків = проста навігація.
    headers = getattr(request, "headers", None)
    if headers is not None and headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def is_loopback_request(request: Request) -> bool:
    """Чи прийшов запит із цього ж комп'ютера. Захисний конверт для дій, що
    керують самою машиною (відкрити теку в Провіднику, поставити оновлення):
    вони мають сенс лише за фізичним ПК, тому мережеві клієнти відсікаються
    навіть з валідною сесією."""
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def require_admin(request: Request, db: Session, *, loopback: bool = True) -> User:
    """Один гейт «це адмін» на весь застосунок (аудит 05.09.26, крок 2.7).

    До цього перевірок було чотири незалежні копії — у `settings.py`, `diag.py`
    і два різні інлайни в роутерах, — і вони встигли розійтись: `/diag/*` пускав
    адміна по мережі, а `/settings/furnaces/password` — ні. Асиметрія була не
    рішенням, а дрейфом. Тепер правило одне, а винятки видно в коді як
    `loopback=False` замість того, щоб губитись між файлами.

    `loopback=True` (типово) — дія керує САМОЮ машиною або її секретами:
    оновлення, паролі пристроїв, шляхи, бекапи, діагностика. Такі речі мають
    сенс лише за фізичним ПК, тож мережевий клієнт відсікається навіть із
    валідною сесією адміна.

    Кидає 401, якщо не ввійшов, і 403 у решті випадків — той самий контракт,
    що був у `require_settings_admin`.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if loopback and not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    return user


def require_admin_or_redirect(request: Request, db: Session, *, loopback: bool = True):
    """Те саме правило, але для роутів, які відповідають СТОРІНКОЮ.

    Різниця лише в одному випадку — «не ввійшов»: форма в Налаштуваннях має
    відвести людину на логін (303), а не віддати голий 401, який HTMX навіть
    не покаже. Саме через цю дрібницю гейт десять разів переписували руками
    замість `require_admin`, і копії почали розходитись (ревʼю 07.09.26, C.5).

    Повертає або користувача, або готову відповідь-редірект — викликач мусить
    її повернути:

        user = require_admin_or_redirect(request, db)
        if isinstance(user, RedirectResponse):
            return user

    Недостатньо прав — і далі 403: це вже не «зайдіть під собою», а відмова.
    """
    if get_current_user(request, db) is None:
        return login_redirect(request)
    return require_admin(request, db, loopback=loopback)


def toast_response(message: str, *, kind: str = "success", triggers: dict | None = None) -> Response:
    """204 + an HX-Trigger toast — the reply for an HTMX action that changed
    something server-side but has nothing to swap into the page. Same
    {"toast": {...}} envelope app.js already listens for. `triggers` adds extra
    HX-Trigger events alongside the toast (e.g. {"refresh-queue": True} to make
    the polled #queue-rows refetch immediately)."""
    payload = {"toast": {"message": message, "kind": kind}}
    if triggers:
        payload.update(triggers)
    response = Response(status_code=204)
    response.headers["HX-Trigger"] = json.dumps(payload)
    return response


def attach_action_toast(response: Response, entry: ActionLog, message: str) -> None:
    """Add a plain success HX-Trigger toast confirming a logged action. Undo is no
    longer offered here — a persistent «Крок назад» button in the queue header
    reverts the last action instead (POST /actions/undo-last), so the toast is
    just confirmation and does not carry an undoUrl.

    ensure_ascii MUST stay on (default): HTTP header values are latin-1, so any
    Cyrillic in `message` has to ride as \\uXXXX escapes — htmx decodes them back
    to real text. ensure_ascii=False here put raw Cyrillic in the header and 500'd
    the whole request. `entry` is kept in the signature for callers/logging parity."""
    response.headers["HX-Trigger"] = json.dumps(
        {"toast": {"message": message, "kind": "success"}}
    )


def attach_sync_error_toast(response: Response, note: str, sync_error: str) -> None:
    """Гучно сказати, що в порталі збережено, а в таблицю НЕ записано.

    Раніше гілка помилки не чіпляла нічого: єдиним слідом лишався трикутник
    у самому рядку, а полл черги перемальовує рядок кожні 15 секунд — і
    попередження зникало разом зі старою розміткою. Для логіста, техніка й
    другого оператора робота при цьому лишалась «можна брати», тобто прямий
    шлях відфрезерувати її вдруге. Тост живе поза #queue-rows і свап його не
    вбиває.

    ensure_ascii лишається увімкненим із тієї ж причини, що й вище."""
    response.headers["HX-Trigger"] = json.dumps(
        {
            "toast": {
                "message": f"{note}: у таблицю НЕ записано — {sync_error}",
                "kind": "error",
            }
        }
    )


_static_root: Path = resource_path("app/static")


def static_ver(relative: str) -> int:
    """mtime of a static file, appended as a `?v=` query string in templates.

    FastAPI's StaticFiles sends no Cache-Control/Expires header, so a
    browser's own heuristic caching can keep serving a stale CSS/JS file
    after a deploy until the user hard-refreshes. Baking the file's own
    mtime into the URL forces a new URL — and a real fetch — every time the
    file's content actually changes, with zero coordination needed.
    """
    try:
        return int((_static_root / relative).stat().st_mtime)
    except OSError:
        return 0


def changelog_md(text: str):
    """Render the only markup a changelog line uses: **bold**. Escapes first, so
    the CHANGELOG.md content can never inject HTML even though it's our own
    trusted file — cheaper to be safe than to reason about it."""
    import re as _re

    from markupsafe import Markup, escape

    escaped = str(escape(text))
    bolded = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return Markup(bolded)


# Бейджі рейки — Jinja-глобали, і кожен відкривав СВОЮ сесію на кожен рендер.
# Один екран малює себе й кілька партіалів, тож на сторінку виходило під
# десяток зайвих сесій до бази, яка лежить на мережевій шарі (ревʼю 07.09.26,
# K.7). Значення тут спільні для всіх (не персональні), а рейку однаково
# оновлює полл, тож дволітерна витримка непомітна оку й прибирає пачку.
_GLOBALS_TTL_SECONDS = 2.0
_globals_lock = threading.Lock()
_globals_cache: dict[str, tuple[float, object]] = {}


def _cached_global(key: str, compute):
    """Порахувати значення бейджа не частіше, ніж раз на `_GLOBALS_TTL_SECONDS`."""
    now = time.monotonic()
    with _globals_lock:
        hit = _globals_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = compute()
    with _globals_lock:
        _globals_cache[key] = (now + _GLOBALS_TTL_SECONDS, value)
    return value


def clear_global_badge_cache() -> None:
    """Скинути кеш бейджів (тести; дія, яка мусить одразу змінити цифру)."""
    with _globals_lock:
        _globals_cache.clear()


def notify_prefs_uncached() -> dict:
    """Popup-notification preferences for base.html, on their own session.

    A Jinja global rather than per-route context: base.html needs these on
    EVERY page, and threading them through two dozen handlers would guarantee
    one gets missed. Three primary-key reads on SQLite per render — cheap.
    Falls back to the defaults if the DB isn't reachable yet (first run), since
    a settings lookup must never keep a page from rendering.
    """
    try:
        db = SessionLocal()
        try:
            return {
                "style": get_notify_style(db),
                "position": get_notify_position(db),
                "events": sorted(get_notify_events(db)),
                # The popup poll follows the sync-speed preset: on Турбо the
                # "технік змінив роботу" alert lands in ~5s, not the fixed 30s —
                # the whole point of the scrap warning is that it is timely.
                "poll_seconds": get_sync_speed()["screen"],
            }
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — never break page render over preferences
        logger.debug("notify_prefs fell back to defaults", exc_info=True)
        return {
            "style": DEFAULT_NOTIFY_STYLE,
            "position": DEFAULT_NOTIFY_POSITION,
            "events": sorted(key for key, _, _, on in NOTIFY_EVENTS if on),
            "poll_seconds": SYNC_SPEED_PRESETS["normal"]["screen"],
        }


def shift_pending_uncached() -> int:
    """Скільки записок передачі зміни ще на дошці — для бейджа в рейці.

    Jinja-глобал зі своєю сесією, а НЕ змінна контексту, і це принципово:
    pending_mail_count ставить лише роут черги, тому бейдж пошти є тільки на
    черзі. Для передачі зміни це рівно хибний результат — о 08:00 оператор
    цілком може відкрити застосунок одразу на /handout (це його перша справа
    дня) і не побачити нічого. Один COUNT на рендер, і збій БД не має права
    завалити сторінку — тому широкий except, як у notify_prefs.
    """
    try:
        db = SessionLocal()
        try:
            return open_note_count(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — бейдж не варт того, щоб ламати рендер
        logger.debug("shift_pending fell back to 0", exc_info=True)
        return 0


# Скільки хвилин активності вважаємо «оператор зараз працює». Пів години —
# щоб пауза на каву не рахувалась виходом, і щоб учорашня зміна не рахувалась
# зовсім.
BUSY_OPERATOR_WINDOW_MINUTES = 30


def busy_operators_uncached() -> int:
    """Скільки операторів щось робили за останні пів години.

    Потрібно для підтвердження «Встановити оновлення»: воно перезапускає
    застосунок, і на спільному цеховому ПК це може обірвати колегу посеред
    прийняття листа чи видачі (аудит 05.09.26, UX 1.10).

    Таблиці живих сесій у застосунку немає, і заводити її заради одного діалогу
    було б занадто: рахуємо за слідом у журналі дій — це те саме «хтось зараз
    працює», лише з точністю до вікна. Число чесно називається «за останні
    N хв» у самому тексті діалогу, щоб ніхто не читав його як «онлайн».
    """
    try:
        from datetime import timedelta

        from sqlalchemy import func, select

        from app.models import ActionLog

        cutoff = utc_now() - timedelta(minutes=BUSY_OPERATOR_WINDOW_MINUTES)
        db = SessionLocal()
        try:
            return db.scalar(
                select(func.count(func.distinct(ActionLog.operator_id))).where(
                    ActionLog.created_at >= cutoff
                )
            ) or 0
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — діалог не варт того, щоб ламати рендер
        logger.debug("busy_operators fell back to 0", exc_info=True)
        return 0


def sync_state_uncached() -> dict | None:
    """Стан синку таблиці для рейки — Jinja-глобал зі своєю сесією.

    До аудиту 05.09.26 (UX 1.8) `_sync_indicator.html` жив лише в шапці Черги,
    бо контекст `sync_status` готував лише її роут. Тобто пауза синку чи
    «немає відповіді» були невидимі з Видачі, Пошти й Архіву — саме там, де
    оператор проводить ранок. Той самий патерн, що shift_pending: власна
    сесія, широкий except, індикатор не сміє завалити рендер.

    `None` — таблиця не налаштована; шаблон тоді нічого не малює.
    """
    try:
        from app.routers.queue import live_sync_status
        from app.services.config_state import sheets_configured

        db = SessionLocal()
        try:
            if not sheets_configured(db):
                return None
            return live_sync_status(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — індикатор не варт того, щоб ламати рендер
        logger.debug("sync_state fell back to None", exc_info=True)
        return None


def feedback_open_count_uncached() -> int:
    """Скільки нових звернень зворотного зв'язку — для бейдра «Звернення» в рейці.

    Той самий патерн, що shift_pending: власна сесія, широкий except, бейдж не
    сміє завалити рендер. Показується лише адмінам (шаблон гейтить), але сам
    підрахунок дешевий COUNT."""
    try:
        from app.services.feedback import open_count

        db = SessionLocal()
        try:
            return open_count(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.debug("feedback_open_count fell back to 0", exc_info=True)
        return 0


#: Ключ дзеркала візуального набору в сесії — див. коментар усередині ui_prefs.
UI_SESSION_KEY = "ui"


def ui_prefs(request: Request) -> dict:
    """Візуальні налаштування залогіненого оператора для base.html:
    {"theme": ""|"forge", "icons": ""|"thin"|"duo"|"bold"|"neon"}.

    Jinja-глобал з request-аргументом (base.html завжди має request у
    контексті): тема мусить бути на <html> у САМОМУ HTML, інакше сторінка
    мигне канонним кольором до першого скрипта. Кеш у request.state — один
    lookup на рендер, а не на кожен виклик у шаблоні. Збій БД чи відсутність
    сесії дає канон і ніколи не ламає рендер (той самий контракт, що
    notify_prefs/shift_pending).
    """
    # request.state може бути відсутній у фейкових request'ах тестів —
    # хелпер мусить пережити БУДЬ-ЯКИЙ request, бо стоїть у base.html.
    # Дзеркало набору в сесії (підписана кука, без БД). Потрібне рівно для
    # одного випадку — сторінки помилки: вона показується САМЕ ТОДІ, коли з
    # базою погано, а тодішній відкат у канон означав, що оператор бачить чужу
    # тему в найгіршу мить і думає, що зламалось іще й оформлення.
    # Джерело правди лишається в БД, кука тільки повторює її останнє значення.
    state = getattr(request, "state", None)
    cached = getattr(state, "ui_prefs_cache", None) if state is not None else None
    if cached is not None:
        return cached
    prefs = {
        # Дефолт теми — "forge" (Amber Forge): бурштиновий вигляд тепер
        # стандартний і для анонімних сторінок (вхід), і для акаунтів, які
        # теми не чіпали. Бірюзовий канон лишається вибором, але має власне
        # значення "teal" (див. account.html) — порожнє більше не «канон за
        # замовчуванням», а «нічого не збережено» → теж forge.
        "theme": "forge",
        "icons": "",
        "buttons": "",
        "loader": "",
        "chips": "",
        "mail_row_pad": 0,
        "mail_list_w": 0,
        "mail_step": 0,
        "queue_density": "",
        "queue_row_pad": 0,
        "queue_mat_style": "",
        "queue_step": 0,
        "handout_layout": "",
        "handout_flow": "",
        # Віджет верстатів: "" = «Пил на сталі» / «Сегменти» (дефолт власника).
        "machine_art": "",
        "machine_strip": "",
        # Картки на екрані «Верстати»: "" = портрет, "frame" = живий кадр у плитці.
        "machine_card": "",
        # Порядок віджетів черги (режим редагування в шестерні вигляду).
        "side_order": "",
        "strip_order": "",
        # Показники стрічки навантаження (шестерня вигляду). "" тут — це
        # дефолт «усі три», бо анонім/збій не має ховати віджет.
        "load_metrics": "crm,pc,ram",
    }
    mirrored = False
    try:
        user_id = request.session.get("user_id")
        if user_id is not None:
            db = SessionLocal()
            try:
                user = db.get(User, user_id)
                if user is not None and user.is_active:
                    prefs = {
                        "theme": user.ui_theme or "forge",
                        "icons": user.ui_icon_style or "",
                        "buttons": user.ui_button_style or "",
                        "loader": user.ui_loader_style or "",
                        "chips": user.ui_chip_style or "",
                        "mail_row_pad": user.mail_row_pad or 0,
                        "mail_list_w": user.mail_list_width or 0,
                        "mail_step": user.mail_ui_step or 0,
                        "queue_density": user.queue_density or "",
                        "queue_row_pad": user.queue_row_pad or 0,
                        "queue_mat_style": user.queue_mat_style or "",
                        "queue_step": user.queue_ui_step or 0,
                        "handout_layout": user.handout_layout or "",
                        "handout_flow": user.handout_flow or "",
                        "machine_art": user.ui_machine_art or "",
                        "machine_strip": user.ui_machine_strip or "",
                        "machine_card": user.ui_machine_card or "",
                        "side_order": user.queue_side_order or "",
                        "strip_order": user.queue_strip_order or "",
                        "load_metrics": user.queue_load_metrics if user.queue_load_metrics is not None else "crm,pc,ram",
                    }
                    mirrored = True
            finally:
                db.close()
            # Пишемо лише коли значення справді змінилось: інакше кожна
            # відповідь тягла б за собою зайвий Set-Cookie.
            if mirrored and request.session.get(UI_SESSION_KEY) != prefs:
                request.session[UI_SESSION_KEY] = dict(prefs)
    except Exception:  # noqa: BLE001 — тема не варта зламаної сторінки
        logger.debug("ui_prefs fell back to defaults", exc_info=True)
    if not mirrored:
        # БД мовчить (або сторінка помилки) — беремо останнє відоме з сесії.
        try:
            saved = request.session.get(UI_SESSION_KEY)
            if isinstance(saved, dict):
                prefs = {key: saved.get(key, prefs[key]) for key in prefs}
        except Exception:  # noqa: BLE001
            logger.debug("ui_prefs session mirror unreadable", exc_info=True)
    if state is not None:
        try:
            state.ui_prefs_cache = prefs
        except Exception:  # noqa: BLE001
            pass
    return prefs


class _TimedTemplates(Jinja2Templates):
    """Jinja2Templates, що рахує час рендера в розкладку запиту.

    Рендер — найбільш недооцінений доданок затримки: у Starlette шаблон
    малюється ЖАДІБНО в `_TemplateResponse.__init__`, тобто всередині роута,
    і на таблиці в кілька сотень рядків це реальні секунди. У логу його не
    було видно взагалі — «Slow request … 4.17s» однаково виглядав і для
    повільного SQL, і для повільного шаблону.

    Ім'я шаблону йде в окрему фазу (`render:queue.html`), бо на одному екрані
    їх кілька, і цікаво, який саме дорогий.
    """

    def TemplateResponse(self, *args, **kwargs):  # noqa: N802 — ім'я з базового класу
        name = ""
        for candidate in args:
            if isinstance(candidate, str) and candidate.endswith(".html"):
                name = candidate
                break
        if not name:
            name = str(kwargs.get("name", ""))
        with perf.span(f"render:{name}" if name else "render"):
            return super().TemplateResponse(*args, **kwargs)


templates = _TimedTemplates(directory=str(resource_path("app/templates")))
def _timed_global(name: str, fn):
    """Обгортка для Jinja-глобала, що ходить у базу.

    Ці чотири глобали викликаються на КОЖНОМУ рендері й кожен відкриває власну
    сесію (свідомо — див. їхні докстрінги). Разом це кілька запитів і кілька
    розшифрувань Fernet на сторінку, і в розкладці вони раніше не з'являлись
    узагалі: час осідав у «total» як нічий. Тепер видно, скільки коштує сама
    обгортка сторінки, окремо від корисної роботи роута.
    """
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        with perf.span(f"globals:{name}"):
            return fn(*args, **kwargs)

    return wrapper


def license_notice_uncached() -> dict | None:
    """Скільки лишилось ліцензії — для смуги над чергою.

    Jinja-глобал, а не контекст роуту: черга віддається з десятка різних
    гілок (фільтри, періоди, HTMX-фрагменти), і протягнути поле крізь усі
    означало б забути його в одній.

    Повертає None у трьох випадках: ліцензія безстрокова, до кінця більше за
    поріг, або її взагалі нема — останнє не наша турбота, бо без валідної
    ліцензії `license_gate` не пускає далі за екран активації, і черги ніхто
    не побачить. Виняток гаситься: попередження не має права покласти екран,
    заради якого оператор і відкрив застосунок.
    """
    try:
        db = SessionLocal()
        try:
            status = get_license_status(db)
        finally:
            db.close()
    except Exception:
        # Широкий except тут свідомий (див. докстрінг), але МОВЧАЗНИЙ він бути
        # не має: саме він з'їв NameError від забутого імпорту, і смуга просто
        # не з'являлась — без сліду ні в лозі, ні на екрані.
        logger.exception("Не вдалося порахувати термін ліцензії для смуги над чергою")
        return None
    if not status.valid or status.expires_at is None:
        return None
    days = (status.expires_at.date() - business_today()).days
    if days > LICENSE_EXPIRY_WARNING_DAYS:
        return None
    # Останній тиждень — червоним: тоді це вже не «варто подбати», а «завтра
    # цех стане». Межа доби робоча (business_today), як і скрізь у застосунку.
    return {
        "days": days,
        "date": status.expires_at.strftime("%d.%m.%Y"),
        "tone": "bad" if days <= 7 else "warn",
    }


# Публічні імена лишаються тими самими — шаблони й тести кличуть їх як раніше,
# просто тепер через спільну витримку (див. `_cached_global`).
def notify_prefs() -> dict:
    return _cached_global("notify_prefs", notify_prefs_uncached)


def license_notice() -> dict | None:
    return _cached_global("license_notice", license_notice_uncached)


def shift_pending() -> int:
    return _cached_global("shift_pending", shift_pending_uncached)


def busy_operators() -> int:
    return _cached_global("busy_operators", busy_operators_uncached)


def sync_state() -> dict | None:
    return _cached_global("sync_state", sync_state_uncached)


def feedback_open_count() -> int:
    return _cached_global("feedback_open_count", feedback_open_count_uncached)

templates.env.globals["perf_id"] = perf.current_request_id
templates.env.globals["is_overdue"] = is_overdue
templates.env.globals["material_color_css_class"] = material_color_css_class
templates.env.globals["material_badge"] = material_badge
templates.env.globals["material_family_class"] = material_family_class
templates.env.globals["material_families"] = material_families
# Статус-крапка й перелік статусів — для рядка черги і легенди кольорів
# (_status_legend.html), з одного джерела app/statuses.py.
templates.env.globals["status_dot"] = status_dot
templates.env.globals["all_statuses"] = STATUSES
templates.env.globals["split_material_color"] = split_material_color
templates.env.globals["strip_material_word"] = strip_material_word
templates.env.globals["triage_readiness"] = triage_readiness
templates.env.globals["files_on_disk"] = files_on_disk
from app.services.machines import machine_model_key  # noqa: E402 — після створення templates
templates.env.globals["machine_model_key"] = machine_model_key
from app.services.machines import MACHINE_MODELS  # noqa: E402
templates.env.globals["machine_models"] = MACHINE_MODELS
from app.services.widget_order import side_index, sort_machine_cards  # noqa: E402


def side_order_index(request, section: str) -> int:
    """CSS `order` секції правої панелі для цього оператора."""
    return side_index(ui_prefs(request).get("side_order"), section)


def ordered_machine_cards(request, cards):
    """Картки верстатів у порядку, який оператор виставив перетягуванням."""
    return sort_machine_cards(ui_prefs(request).get("strip_order"), list(cards or []))


templates.env.globals["side_order_index"] = side_order_index
templates.env.globals["ordered_machine_cards"] = ordered_machine_cards
from app.services.widget_order import load_metrics_set  # noqa: E402


def load_metrics(request) -> set:
    """Множина показників стрічки навантаження для цього оператора."""
    return load_metrics_set(ui_prefs(request).get("load_metrics"))


templates.env.globals["load_metrics"] = load_metrics
templates.env.globals["is_rush_comment"] = is_rush_comment
templates.env.globals["static_ver"] = static_ver
templates.env.filters["changelog_md"] = changelog_md
# Наївний UTC із бази (server_default на SQLite) → київський час для показу.
# Журнал синку показував Гринвіч, і о 20:17 «останній запис 17:15» читався
# як три години мовчання синку (08.09.26).
templates.env.filters["kyiv"] = utc_to_business
# Available in every template without every route threading it through its
# own context dict — same rationale as static_ver above. Reads the
# in-memory "last known result" (see app/update_check.py::get_known_update),
# never touches the network from a request-handling thread.
templates.env.globals["get_known_update"] = get_known_update
templates.env.globals["license_notice"] = license_notice
# Product version, available in every template (rail foot, settings "about")
# without threading it through each route's context — same rationale as the
# globals above. Single source of truth is app/__version__.py.
templates.env.globals["app_version"] = VERSION
# Плити стану розділів налаштувань. Порожній словник = «даних немає»:
# макрос _settings_slab.html малює лише назву й підзаголовок. Потрібен як
# глобал, бо партіали _settings_*.html рендеряться і поза /settings —
# тестами й майбутніми фрагментами, де повного контексту немає.
templates.env.globals["slabs"] = {}
# Меню налаштувань і права на розділ — з реєстру `app/services/settings_nav.py`.
# Саме глобали, а не контекст роута: рейка `_topbar_nav.html` включається на
# десятку сторінок (акаунт, матеріали, журнал…), і передавати меню в кожен
# контекст означало б знову мати кілька місць, які мусять збігатися.
templates.env.globals["settings_nav"] = visible_nav
templates.env.globals["can_see"] = can_see
templates.env.globals["can_edit"] = can_edit
templates.env.globals["nav_payload"] = nav_payload
templates.env.globals["notify_prefs"] = _timed_global("notify_prefs", notify_prefs)
templates.env.globals["shift_pending"] = _timed_global("shift_pending", shift_pending)
templates.env.globals["feedback_open_count"] = _timed_global("feedback_open_count", feedback_open_count)
templates.env.globals["busy_operators"] = _timed_global("busy_operators", busy_operators)
templates.env.globals["sync_state"] = _timed_global("sync_state", sync_state)
templates.env.globals["ui_prefs"] = _timed_global("ui_prefs", ui_prefs)
templates.env.filters["night_label"] = night_label
