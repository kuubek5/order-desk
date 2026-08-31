"""Спільна база HTTP-шару: сесія БД, поточний оператор, шаблони, тости.

Кожен роутер імпортує звідси, а не з `app.web` — інакше вийшло б кільце
(web підключає роутери, роутери тягли б web назад). Тому цей модуль НЕ
імпортує ні `app.web`, ні жоден роутер.
"""

import ipaddress
import json
import logging
from pathlib import Path

from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.__version__ import VERSION
from app.db import SessionLocal
from app.material_class import (
    material_badge,
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
from app.services.shift import night_label, open_note_count
from app.statuses import is_overdue
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
    return user


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


def notify_prefs() -> dict:
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


def shift_pending() -> int:
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
        "theme": "",
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
                        "theme": user.ui_theme or "",
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


templates = Jinja2Templates(directory=str(resource_path("app/templates")))
templates.env.globals["is_overdue"] = is_overdue
templates.env.globals["material_color_css_class"] = material_color_css_class
templates.env.globals["material_badge"] = material_badge
templates.env.globals["material_family_class"] = material_family_class
templates.env.globals["split_material_color"] = split_material_color
templates.env.globals["strip_material_word"] = strip_material_word
templates.env.globals["triage_readiness"] = triage_readiness
templates.env.globals["files_on_disk"] = files_on_disk
templates.env.globals["is_rush_comment"] = is_rush_comment
templates.env.globals["static_ver"] = static_ver
templates.env.filters["changelog_md"] = changelog_md
# Available in every template without every route threading it through its
# own context dict — same rationale as static_ver above. Reads the
# in-memory "last known result" (see app/update_check.py::get_known_update),
# never touches the network from a request-handling thread.
templates.env.globals["get_known_update"] = get_known_update
# Product version, available in every template (rail foot, settings "about")
# without threading it through each route's context — same rationale as the
# globals above. Single source of truth is app/__version__.py.
templates.env.globals["app_version"] = VERSION
templates.env.globals["notify_prefs"] = notify_prefs
templates.env.globals["shift_pending"] = shift_pending
templates.env.globals["ui_prefs"] = ui_prefs
templates.env.filters["night_label"] = night_label
