from contextlib import asynccontextmanager
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import ipaddress
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import Annotated
import uuid
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from imap_tools import MailBox
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app.auth import hash_password, verify_password
from app.backup import BackupFormatError, BackupPasswordError, create_backup, restore_backup
from app.client_matcher import match_client_name
from app.client_profile import find_matching_orders, summarize_client_orders
from app.config import MAIL_ATTACHMENTS_PATH, SESSION_SECRET_KEY
from app.db import Base, SessionLocal, engine
from app.export_scanner import scan_export_folder
from app.license import get_license_status, get_machine_id, verify_license_key
from app.mail_export import save_attachments_to_export
from app.mail_reader import IMAP_HOST, IMAP_TIMEOUT_SECONDS
from app.mail_sync_service import MailSyncBusyError, MailSyncError, sync_mail_background, sync_mailbox
from app.material_class import material_color_css_class
from app.models import Client, ClientNameAlias, Comment, EmailMessage, Order, ReworkRecord, StatusEvent, SyncLog, User
from app.order_folder import (
    attach_email_folder_availability,
    attach_email_preview_tokens,
    attach_export_folder_uris,
    attach_job_code_folder_uris,
    folder_to_file_uri,
    resolve_email_attachment_folder,
)
from app.queue_filters import (
    HANDOUT_SOURCE_FILTERS,
    READY_FILTERS,
    SERVICE_TYPE_FILTERS,
    SOURCE_FILTERS,
    count_by_readiness,
    count_by_service_type,
    count_by_source,
    count_client_groups_by_source,
    filter_by_readiness,
    filter_by_source,
    filter_client_groups_by_source,
    filter_emails_by_service_type,
)
from app.runtime import resource_path
from app.settings_store import (
    OPERATOR_EDITABLE_KEYS,
    SETTING_FIELDS,
    get_all_settings,
    get_export_folder_path,
    get_google_service_account_json,
    get_google_sheet_id,
    get_imap_login,
    get_imap_password,
    set_setting,
)
from app.sheet_sync_service import (
    SheetSyncBusyError,
    SheetSyncError,
    SheetSyncSummary,
    sync_google_sheets,
    sync_sheets_background,
)
from app.sheet_writer import (
    append_mail_placeholder_row,
    append_order_comment,
    apply_status_markers,
    write_order_fields,
)
from app.sheets import get_worksheet_by_name, open_spreadsheet
from app.statuses import STATUSES, is_overdue
from app.stats import average_new_to_milled_hours, parse_int_safe, summarize_rework_by_blame
from app.stl_preview import build_preview_token, list_stl_files, resolve_preview_folder, resolve_stl_file
from app.update_check import (
    _update_check_worker,
    download_and_verify,
    get_known_update,
    launch_silent_install,
)

try:
    BUSINESS_TIMEZONE = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:  # Windows Python may not bundle the IANA tz database.
    BUSINESS_TIMEZONE = None


_FIRST_ADMIN_LOCK = Lock()
logger = logging.getLogger(__name__)
MAIL_SYNC_INTERVAL_SECONDS = 2 * 60
MAIL_SYNC_INITIAL_DELAY_SECONDS = 10
# One sync cycle costs ~4 Google Sheets API calls (spreadsheet.worksheets() +
# get_all_values() per relevant tab, typically 3 tabs) — Google's quota is
# hundreds of reads/minute, far above that. The old 2-minute value was never
# based on a real technical constraint, it was just copied from
# MAIL_SYNC_INTERVAL_SECONDS above; confirmed safe to halve so the queue
# reflects sheet edits sooner.
SHEET_SYNC_INTERVAL_SECONDS = 1 * 60
SHEET_SYNC_INITIAL_DELAY_SECONDS = 10

# A heartbeat last-attempt older than this many sync intervals means the
# background loop itself likely died (thread crashed, process wedged) rather
# than just "ran and found nothing" — the scariest failure mode, since it's
# the monitoring signal silently going quiet. See _sync_heartbeat_status.
STALE_HEARTBEAT_MULTIPLIER = 3


@dataclass(frozen=True)
class SyncHeartbeat:
    """Last-tick outcome of a background sync loop (mail IMAP or Google
    Sheets), kept in memory only — this is a liveness signal ("is the loop
    ticking right now"), not an audit trail. SyncLog remains the audit
    trail and deliberately writes no row for a quiet/no-op background tick
    or a background-triggered failure (see mail_sync_service.sync_mail /
    sheet_sync_service.sync_google_sheets's `persist=trigger == "manual"`),
    so silence there is ambiguous between "healthy and quiet" and "dead".
    This heartbeat is updated on every single tick regardless, to remove
    that ambiguity. Not persisted to the DB and does not survive a
    restart — showing "unknown" until the next tick completes after a
    restart is correct and honest, not a bug.
    """

    last_attempt_at: datetime | None = None
    status: str = "unknown"  # "unknown" | "ok" | "error" | "skipped"
    error_message: str | None = None


# Keyed by sync type. Only the matching background worker thread ever writes
# its own key (mail worker writes "mail", sheet worker writes "sheet"), so
# there is never more than one writer per key and a Lock isn't needed for
# the write side. Request-handling threads only read this dict to render the
# queue page. Each write below swaps in a brand-new *immutable* SyncHeartbeat
# instance in one dict-key assignment — under the GIL that single assignment
# is atomic, so a concurrent reader always sees either the old or the new
# heartbeat in full, never a partially-updated one. Do not turn this into a
# multi-step mutation (e.g. `heartbeat.status = ...`) — that would reopen the
# torn-read risk this comment is explaining away.
_sync_heartbeats: dict[str, SyncHeartbeat] = {
    "mail": SyncHeartbeat(),
    "sheet": SyncHeartbeat(),
}


def _record_sync_heartbeat(key: str, *, status: str, error_message: str | None = None) -> None:
    """Record one background sync tick's outcome for the queue page's status pair.

    ``status="skipped"`` (another sync already holds the lock — MailSyncBusyError /
    SheetSyncBusyError) is deliberately neutral: it proves the loop is alive
    (last_attempt_at advances, which is what staleness detection cares about)
    without overwriting a previously recorded real outcome with a false error.
    """
    now = datetime.now()
    if status == "skipped":
        previous = _sync_heartbeats[key]
        _sync_heartbeats[key] = SyncHeartbeat(
            last_attempt_at=now, status=previous.status, error_message=previous.error_message
        )
    else:
        _sync_heartbeats[key] = SyncHeartbeat(
            last_attempt_at=now, status=status, error_message=error_message
        )


def _relative_time_uk(reference: datetime, now: datetime) -> str:
    """"N хв тому" / "N год тому" — no reusable relative-time helper exists
    elsewhere in this codebase (received_at etc. are all rendered as absolute
    "%d.%m.%y %H:%M" timestamps), so this is a small new one."""
    seconds = max(0, int((now - reference).total_seconds()))
    minutes = seconds // 60
    if minutes < 1:
        return "щойно"
    if minutes < 60:
        unit = _pluralize_uk(minutes, "хвилину", "хвилини", "хвилин")
        return f"{minutes} {unit} тому"
    hours = minutes // 60
    unit = _pluralize_uk(hours, "годину", "години", "годин")
    return f"{hours} {unit} тому"


def _sync_heartbeat_status(
    heartbeat: SyncHeartbeat,
    *,
    configured: bool,
    interval_seconds: int,
    now: datetime,
) -> dict[str, str]:
    """Pure formatting for one sync-status line in the queue sidebar.

    Precedence: unconfigured beats everything (nothing is supposed to be
    running, so silence isn't a warning sign) — then staleness (see
    STALE_HEARTBEAT_MULTIPLIER) beats whatever outcome was last recorded,
    because a dead worker thread is worse than a recorded failure — only
    then do we fall back to the last real success/error tick.
    """
    if not configured:
        return {"state": "neutral", "label": "не налаштовано"}
    if heartbeat.last_attempt_at is None:
        return {"state": "neutral", "label": "очікує першої перевірки"}

    age_seconds = max(0.0, (now - heartbeat.last_attempt_at).total_seconds())
    if age_seconds > interval_seconds * STALE_HEARTBEAT_MULTIPLIER:
        return {"state": "warning", "label": "⚠ немає відповіді від фонового процесу"}

    relative = _relative_time_uk(heartbeat.last_attempt_at, now)
    if heartbeat.status == "error":
        return {"state": "error", "label": f"⚠ помилка · {relative}"}
    if heartbeat.status == "ok":
        return {"state": "success", "label": f"✓ {relative}"}
    # "skipped" with no prior real outcome yet (busy on the very first tick
    # this process ever attempted) — rare, but still an honest "unknown".
    return {"state": "neutral", "label": "очікує результату"}


def _queue_sync_status(db: Session, now: datetime) -> dict[str, dict[str, str]]:
    return {
        "mail": _sync_heartbeat_status(
            _sync_heartbeats["mail"],
            configured=_imap_configured(db),
            interval_seconds=MAIL_SYNC_INTERVAL_SECONDS,
            now=now,
        ),
        "sheet": _sync_heartbeat_status(
            _sync_heartbeats["sheet"],
            configured=_sheets_configured(db),
            interval_seconds=SHEET_SYNC_INTERVAL_SECONDS,
            now=now,
        ),
    }


def _is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _open_folder_in_explorer(folder: Path) -> None:
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise NotImplementedError
    os.startfile(str(folder))  # type: ignore[attr-defined]


def _check_path_status(raw_path: str) -> dict[str, str]:
    """Live filesystem probe for the Налаштування path fields (export_folder_path /
    technician_files_path — CLAUDE.md section 7 "мобільним під різні ситуації та ПК").

    A saved path is just a string; nothing else in the save flow checks that it
    actually resolves on *this* machine. `os.access(path, os.W_OK)` is not trusted
    alone — it is unreliable for some Windows network shares — so writability is
    proven with a real create+delete of a small marker file, mirroring the
    defensive try/except style already used in app/mail_export.py.
    """
    value = (raw_path or "").strip()
    if not value:
        return {"state": "neutral", "message": ""}

    path = Path(value)
    try:
        exists = path.exists()
    except OSError:
        return {
            "state": "error",
            "message": "Шлях недоступний на цьому комп'ютері — перевірте диск або мережу",
        }

    if not exists:
        return {
            "state": "error",
            "message": "Шлях не знайдено — можливо, диск не підключений на цьому ПК або є одруківка",
        }
    if not path.is_dir():
        return {
            "state": "error",
            "message": "Це не папка — вказано файл замість каталогу",
        }

    marker = path / f".orderdesk-check-{uuid.uuid4().hex}.tmp"
    try:
        marker.write_bytes(b"")
    except OSError:
        return {
            "state": "warning",
            "message": "Папку знайдено, але немає прав на запис — читання може працювати, збереження файлів — ні",
        }
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass

    return {"state": "success", "message": "Папку знайдено, доступна для запису"}


def _mail_trusted_roots(db: Session) -> list[Path]:
    roots: list[Path] = []
    mail_root = str(MAIL_ATTACHMENTS_PATH).strip()
    export_root = (get_export_folder_path(db) or "").strip()
    if mail_root:
        roots.append(Path(mail_root))
    if export_root:
        roots.append(Path(export_root))
    return roots


def _mail_preview_roots(db: Session) -> dict[str, str | None]:
    return {"mail": str(MAIL_ATTACHMENTS_PATH), "export": get_export_folder_path(db)}


def _sheets_configured(db: Session) -> bool:
    return bool(
        (get_google_sheet_id(db) or "").strip()
        and (get_google_service_account_json(db) or "").strip()
    )


def _imap_configured(db: Session) -> bool:
    return bool((get_imap_login(db) or "").strip() and (get_imap_password(db) or "").strip())


def _mail_sync_tick(db: Session) -> None:
    """One background IMAP sync attempt: gated on IMAP being configured
    (an unconfigured sync is not attempted, and correctly stays "не
    налаштовано" rather than touching the heartbeat). Records a
    SyncHeartbeat for success or either well-known failure mode
    (MailSyncBusyError / MailSyncError); any other exception is left to
    propagate to _mail_sync_worker's own generic handler below, which
    records its own failure heartbeat — same three-way dispatch the
    original inline try/except used, just factored out so it is directly
    callable from tests without threads/Event. See SyncHeartbeat's
    docstring for why this exists alongside SyncLog.
    """
    if not _imap_configured(db):
        return
    try:
        sync_mail_background(db, Path(MAIL_ATTACHMENTS_PATH))
    except MailSyncBusyError:
        _record_sync_heartbeat("mail", status="skipped")
        return
    except MailSyncError as exc:
        logger.warning("Background mail sync failed: %s", exc)
        _record_sync_heartbeat("mail", status="error", error_message=str(exc))
        return
    _record_sync_heartbeat("mail", status="ok")


def _mail_sync_worker(stop_event: Event) -> None:
    """Poll IMAP without occupying the web request loop or delaying shutdown."""
    if stop_event.wait(MAIL_SYNC_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                _mail_sync_tick(db)
        except Exception:
            logger.exception("Unexpected background mail sync failure")
            _record_sync_heartbeat(
                "mail", status="error", error_message="Неочікувана помилка синхронізації пошти"
            )

        stop_event.wait(MAIL_SYNC_INTERVAL_SECONDS)


def _sheet_sync_tick(db: Session) -> None:
    """One background Google Sheets sync attempt — same gate/dispatch/testing
    rationale as _mail_sync_tick above, for SheetSyncBusyError / SheetSyncError."""
    if not _sheets_configured(db):
        return
    try:
        sync_sheets_background(db)
    except SheetSyncBusyError:
        _record_sync_heartbeat("sheet", status="skipped")
        return
    except SheetSyncError as exc:
        logger.warning("Background sheet sync failed: %s", exc)
        _record_sync_heartbeat("sheet", status="error", error_message=str(exc))
        return
    _record_sync_heartbeat("sheet", status="ok")


def _sheet_sync_worker(stop_event: Event) -> None:
    """Poll Google Sheets without occupying the web request loop or delaying
    shutdown — same shape as _mail_sync_worker above. Table rows are entered
    by trusted internal staff (technologists/admins), not free-text clients,
    so unlike email there is no per-row guess to review before it becomes an
    Order: sync_tab already writes directly. Automating the periodic pull
    just removes the "someone has to remember to click Синхронізувати" step."""
    if stop_event.wait(SHEET_SYNC_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                _sheet_sync_tick(db)
        except Exception:
            logger.exception("Unexpected background sheet sync failure")
            _record_sync_heartbeat(
                "sheet", status="error", error_message="Неочікувана помилка синхронізації таблиці"
            )

        stop_event.wait(SHEET_SYNC_INTERVAL_SECONDS)


def _sync_summary_message(summary: SheetSyncSummary) -> str:
    if summary.tabs_processed == 0:
        return "Підключення працює, але в доступному періоді не знайдено датованих вкладок."
    return (
        f"Синхронізовано вкладок: {summary.tabs_processed}. "
        f"Нових робіт: {summary.created}, оновлено: {summary.updated}, "
        f"без змін: {summary.unchanged}."
    )


def _parse_sheet_tab(sheet_tab: str | None) -> date | None:
    if not sheet_tab:
        return None
    try:
        return datetime.strptime(sheet_tab, "%d.%m.%y").date()
    except ValueError:
        return None


def _order_date(order: Order) -> date:
    """Business date for both sheet and email sourced orders."""
    sheet_date = _parse_sheet_tab(order.sheet_tab)
    if sheet_date is not None:
        return sheet_date
    if order.created_at is not None:
        created_utc = order.created_at.replace(tzinfo=timezone.utc)
        if BUSINESS_TIMEZONE is not None:
            return created_utc.astimezone(BUSINESS_TIMEZONE).date()
        # Europe/Kyiv follows the EU transition rule. This fallback keeps the
        # app usable before `tzdata` is installed in a Windows development venv.
        year = created_utc.year
        march_last_sunday = 31 - (calendar.weekday(year, 3, 31) + 1) % 7
        october_last_sunday = 31 - (calendar.weekday(year, 10, 31) + 1) % 7
        dst_start = datetime(year, 3, march_last_sunday, 1, tzinfo=timezone.utc)
        dst_end = datetime(year, 10, october_last_sunday, 1, tzinfo=timezone.utc)
        offset = timedelta(hours=3 if dst_start <= created_utc < dst_end else 2)
        return (created_utc + offset).date()
    return date.today()


def _queue_sort_key(order: Order) -> tuple:
    """Oldest overdue work first, then the earliest daily deadline."""
    due_rank = {"09:00": 0, "14:00": 1, "16:00": 2}.get(order.due_time, 3)
    overdue_rank = 0 if is_overdue(order.sheet_tab, order.status) else 1
    return overdue_rank, _order_date(order), due_rank, order.id


# Column headers the operator can click to sort the queue table (queue.html
# thead, via _sortable_th.html). Explicit, opt-in — with no `sort` query
# param the queue keeps its default urgency-based _queue_sort_key ordering.
QUEUE_SORT_FIELDS = ("material", "kind", "quantity")


def _queue_column_sort_value(order: Order, sort: str) -> int | str | None:
    """Sort value for one column, or None for "blank" (missing/unparseable)
    — callers must always sort blanks last, never first, regardless of
    direction (an operator sorting "by material" doesn't want blanks at the
    top just because they picked descending)."""
    if sort == "quantity":
        # Order.quantity is a free-text Mapped[Optional[str]] column (see
        # app/models.py), not a number — reuse the same defensive parser
        # app/stats.py already established for this exact field instead of
        # writing a third copy of "parse this string as an int or give up".
        return parse_int_safe(order.quantity)
    field = "material_color" if sort == "material" else "kind"
    value = getattr(order, field, None)
    if value is None or not value.strip():
        return None
    return value.strip().lower()


def _sort_orders_by_column(orders: list[Order], sort: str, direction: str) -> list[Order]:
    """Stable sort by one queue column. Blank/unparseable values always sort
    last, in both directions — only the *present* values reverse order."""
    reverse = direction == "desc"
    paired = [(order, _queue_column_sort_value(order, sort)) for order in orders]
    present = sorted((p for p in paired if p[1] is not None), key=lambda p: p[1], reverse=reverse)
    missing = [order for order, value in paired if value is None]
    return [order for order, _ in present] + missing


DATE_STRIP_WINDOW = 7


def _known_order_dates(db: Session) -> list[date]:
    """Calendar days that actually have order data, derived straight from
    `Order.sheet_tab` — the same column `app/sync.py` populates verbatim
    from real Google Sheet tab names, and that `accept_email` stamps with
    the Kyiv business date for mail-sourced orders. There is deliberately no
    separate mechanism here that talks to Google Sheets to list its tabs:
    `sheet_tab` already mirrors that list, refreshed every background sync
    tick, so the queue's day-strip stays in sync "for free"."""
    tabs = db.scalars(select(Order.sheet_tab).where(Order.sheet_tab.isnot(None)).distinct()).all()
    parsed = {d for d in (_parse_sheet_tab(tab) for tab in tabs) if d is not None}
    return sorted(parsed)


def _date_window(
    known_dates: list[date], today: date, date_page: int | None, window: int = DATE_STRIP_WINDOW
) -> tuple[list[date], int, int]:
    """Page through `known_dates` (ascending) `window` days at a time.

    Returns `(visible_dates, current_page, total_pages)`. With no explicit
    `date_page`, the default window is the one containing `today`, or the
    most recent window if today has no data yet (e.g. the operator opens
    the queue before the lab has entered anything for today)."""
    if not known_dates:
        return [], 0, 0

    total_pages = (len(known_dates) + window - 1) // window

    if date_page is None:
        anchor_idx = known_dates.index(today) if today in known_dates else len(known_dates) - 1
        current_page = anchor_idx // window
    else:
        current_page = max(0, min(date_page, total_pages - 1))

    start = current_page * window
    return known_dates[start : start + window], current_page, total_pages


def _pluralize_uk(n: int, one: str, few: str, many: str) -> str:
    """Ukrainian has three plural forms selected by the last one/two digits
    of the count (1 клієнт, 2 клієнти, 5 клієнтів, 11 клієнтів, 21 клієнт…)."""
    n_mod_100 = n % 100
    n_mod_10 = n % 10
    if n_mod_10 == 1 and n_mod_100 != 11:
        return one
    if 2 <= n_mod_10 <= 4 and not (12 <= n_mod_100 <= 14):
        return few
    return many


def _handout_pending_client_count(orders: list[Order], today: date) -> int:
    """Distinct clients with an outstanding (pre-today, not yet issued) order —
    the same candidate rule get_handout groups by, minus the filesystem scan,
    so the queue dashboard's KPI/peek cards stay cheap on every page load."""
    clients: set[str] = set()
    for order in orders:
        if not order.client_name or order.status == "видано":
            continue
        order_date = _parse_sheet_tab(order.sheet_tab)
        if order_date is not None and order_date >= today:
            continue
        clients.add(order.client_name)
    return len(clients)


def _queue_handout_summary(orders: list[Order], today: date) -> str:
    count = _handout_pending_client_count(orders, today)
    if count == 0:
        return "Усе видано"
    noun = _pluralize_uk(count, "клієнт очікує", "клієнти очікують", "клієнтів очікують")
    return f"{count} {noun}"


def _queue_week_summary(db: Session, all_orders: list[Order], today: date) -> str:
    """Compact "quantity milled · rework %" line for the Статистика peek card.

    Reuses the order list get_queue already fetched instead of re-running
    get_stats's full scan, plus one light ReworkRecord query scoped to the
    same window — a summary card, not a duplicate of the stats screen.
    """
    week_start = today - timedelta(days=6)
    week_orders = [o for o in all_orders if week_start <= _order_date(o) <= today]
    quantities = (parse_int_safe(o.quantity) for o in week_orders)
    quantity_sum = sum(q for q in quantities if q is not None)

    week_records = db.scalars(
        select(ReworkRecord).where(
            ReworkRecord.created_at >= datetime.combine(week_start, datetime.min.time())
        )
    ).all()
    redo_quantities = (parse_int_safe(r.redo_quantity) for r in week_records)
    redo_sum = sum(q for q in redo_quantities if q is not None)

    if quantity_sum == 0:
        return "Ще немає даних за тиждень"
    if redo_sum == 0:
        return f"{quantity_sum} од. · без браку"
    rework_pct = round(redo_sum / quantity_sum * 100)
    return f"{quantity_sum} од. · брак {rework_pct}%"


def _queue_sync_summary(db: Session) -> str:
    """Last Google Sheets import outcome for the queue dashboard's peek card."""
    last_sync = db.scalar(
        select(SyncLog)
        .where(SyncLog.direction == "sheet_to_db")
        .order_by(SyncLog.occurred_at.desc())
        .limit(1)
    )
    if last_sync is None:
        return "Ще не синхронізовано"
    time_label = last_sync.occurred_at.strftime("%H:%M") if last_sync.occurred_at else "—"
    if last_sync.status == "error":
        return f"Помилка синхронізації ({time_label})"
    return f"Синхронізовано {time_label}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.environ.get("ORDER_DESK_SCHEMA_MANAGED") != "1":
        Base.metadata.create_all(engine)
    mail_stop_event = Event()
    mail_thread = Thread(
        target=_mail_sync_worker,
        args=(mail_stop_event,),
        name="order-desk-mail-sync",
        daemon=True,
    )
    mail_thread.start()
    sheet_stop_event = Event()
    sheet_thread = Thread(
        target=_sheet_sync_worker,
        args=(sheet_stop_event,),
        name="order-desk-sheet-sync",
        daemon=True,
    )
    sheet_thread.start()
    update_check_stop_event = Event()
    update_check_thread = Thread(
        target=_update_check_worker,
        args=(update_check_stop_event,),
        name="order-desk-update-check",
        daemon=True,
    )
    update_check_thread.start()
    try:
        yield
    finally:
        mail_stop_event.set()
        mail_thread.join(timeout=1)
        sheet_stop_event.set()
        sheet_thread.join(timeout=1)
        update_check_stop_event.set()
        update_check_thread.join(timeout=1)


app = FastAPI(title="Order Desk", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    same_site="strict",
    https_only=False,  # Loopback-only HTTP; no network listener is opened.
    max_age=8 * 60 * 60,
)


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    """Log only user-visible stalls, without adding noise for normal requests."""
    started_at = monotonic()
    try:
        return await call_next(request)
    finally:
        duration = monotonic() - started_at
        if duration >= 1.0:
            logger.warning(
                "Slow request: %s %s took %.3fs",
                request.method,
                request.url.path,
                duration,
            )


# Paths that must work with zero license, zero session, zero DB assumptions:
# /health is polled by the release-workflow smoke test (.github/workflows/release.yml)
# before any activation happens, and /static serves the CSS/JS the /license
# screen itself needs to render.
_LICENSE_EXEMPT_PATH_PREFIXES = ("/static/",)
_LICENSE_EXEMPT_PATHS = ("/health", "/license")


@app.middleware("http")
async def license_gate(request: Request, call_next):
    """Block the entire application — even /setup and /login — without a valid license.

    Runs outermost (defined after, so registered last / wraps everything else,
    see Starlette's LIFO middleware stack) and reads its own DB session rather
    than depending on get_db, since dependency injection isn't available here.
    """
    path = request.url.path
    if path in _LICENSE_EXEMPT_PATHS or path.startswith(_LICENSE_EXEMPT_PATH_PREFIXES):
        return await call_next(request)

    db = SessionLocal()
    try:
        status = get_license_status(db)
    finally:
        db.close()

    if not status.valid:
        return RedirectResponse("/license", status_code=303)
    return await call_next(request)


_static_root = resource_path("app/static")


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


templates = Jinja2Templates(directory=str(resource_path("app/templates")))
templates.env.globals["is_overdue"] = is_overdue
templates.env.globals["material_color_css_class"] = material_color_css_class
templates.env.globals["static_ver"] = static_ver
# Available in every template without every route threading it through its
# own context dict — same rationale as static_ver above. Reads the
# in-memory "last known result" (see app/update_check.py::get_known_update),
# never touches the network from a request-handling thread.
templates.env.globals["get_known_update"] = get_known_update

app.mount("/static", StaticFiles(directory=str(resource_path("app/static"))), name="static")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Process-level probe without exposing configuration or mutating the DB."""
    return {"status": "ok"}


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


def _user_count(db: Session) -> int:
    """Return the number of configured accounts without loading user records."""
    return db.scalar(select(func.count()).select_from(User)) or 0


def _validate_first_admin(
    username: str,
    full_name: str,
    password: str,
    password_confirmation: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Normalize and validate the one-time first administrator form."""
    values = {
        "username": username.strip(),
        "full_name": full_name.strip(),
        "password": password,
    }
    if not values["username"] or not values["full_name"]:
        return None, "Вкажіть логін та ім’я адміністратора"
    if len(password) < 10:
        return None, "Пароль має містити щонайменше 10 символів"
    if password != password_confirmation:
        return None, "Паролі не збігаються"
    return values, None


def _write_sheet_fields(db: Session, order: Order, fields: set[str]) -> str | None:
    """Write explicit portal changes and record the outcome without hiding it.

    source == "lab" is the real gate, not sheet_tab truthiness: email orders
    now also carry a sheet_tab-shaped business date (set at accept time, see
    accept_email) so they date-bucket/overdue exactly like table orders, but
    they were never a row in the shared spreadsheet and must never trigger a
    write there — that guard used to be "sheet_tab is set", which silently
    stops being true once email orders have a sheet_tab too.
    """
    if not fields or order.source != "lab" or not order.sheet_tab:
        return None
    try:
        worksheet = get_worksheet_by_name(open_spreadsheet(db=db), order.sheet_tab)
        if worksheet is None:
            raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
        write_order_fields(worksheet, order, fields)
        db.add(
            SyncLog(
                direction="db_to_sheet",
                sheet_tab=order.sheet_tab,
                status="ok",
                message=f"order {order.id}: {', '.join(sorted(fields))}",
            )
        )
        return None
    except Exception as exc:
        error = str(exc)
        db.add(
            SyncLog(
                direction="db_to_sheet",
                sheet_tab=order.sheet_tab,
                status="error",
                message=f"order {order.id}: {error}",
            )
        )
        return error


@app.get("/license", response_class=HTMLResponse)
def license_form(request: Request, db: Session = Depends(get_db)):
    status = get_license_status(db)
    return templates.TemplateResponse(
        request, "license.html", {"status": status, "machine_id": get_machine_id()}
    )


@app.post("/license", response_class=HTMLResponse)
async def license_submit(
    request: Request, license_key: str = Form(""), db: Session = Depends(get_db)
):
    machine_id = get_machine_id()
    status = verify_license_key(license_key, machine_id)
    if not status.valid:
        return templates.TemplateResponse(
            request,
            "license.html",
            {
                "status": status,
                "machine_id": machine_id,
                "error": status.reason,
                "license_key_input": license_key.strip(),
            },
            status_code=400,
        )

    set_setting(db, "license_key", license_key.strip())
    db.commit()

    destination = "/setup" if _user_count(db) == 0 else "/"
    return RedirectResponse(destination, status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    if _user_count(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    if get_current_user(request, db) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db)):
    if _user_count(db) != 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {})


@app.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(""),
    full_name: str = Form(""),
    password: str = Form(""),
    password_confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    if _user_count(db) != 0:
        return RedirectResponse("/login", status_code=303)

    values, error = _validate_first_admin(
        username, full_name, password, password_confirmation
    )
    if error is not None:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": error, "username": username.strip(), "full_name": full_name.strip()},
            status_code=400,
        )

    # The desktop build runs one application process. The lock keeps two local
    # first-run submissions from both passing the empty-database check.
    with _FIRST_ADMIN_LOCK:
        if _user_count(db) != 0:
            return RedirectResponse("/login", status_code=303)
        assert values is not None
        user = User(
            username=values["username"],
            full_name=values["full_name"],
            password_hash=hash_password(values["password"]),
            role="адмін",
            is_active=True,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return RedirectResponse("/login", status_code=303)
        db.refresh(user)

    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/settings?welcome=1", status_code=303)


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    if _user_count(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Невірний логін або пароль"}
        )
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/account", response_class=HTMLResponse)
async def get_account(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse(request, "account.html", {"user": user})


@app.post("/account/password", response_class=HTMLResponse)
async def post_account_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    error = None
    if not verify_password(current_password, user.password_hash):
        error = "Поточний пароль невірний"
    elif len(new_password) < 6:
        error = "Новий пароль має бути не коротшим за 6 символів"
    elif new_password != confirm_password:
        error = "Паролі не збігаються"

    if error:
        return templates.TemplateResponse(request, "account.html", {"user": user, "error": error})

    user.password_hash = hash_password(new_password)
    db.commit()

    return templates.TemplateResponse(request, "account.html", {"user": user, "saved": True})


@app.get("/", response_class=HTMLResponse)
def get_queue(
    request: Request,
    period: str = "today",
    ready: str = "all",
    source: str = "all",
    overdue: str = "0",
    # `date` (query key) can't be the python parameter name — it would
    # shadow the `date` class imported at module level and used throughout
    # this function (`date.today()` etc). `Annotated` keeps the *default*
    # value a plain `""`/`None` (not a `Query(...)` sentinel object), so
    # calling `get_queue(...)` directly in tests — the established pattern
    # in this file, see tests/test_mail_queue_backend.py — still works
    # without going through FastAPI's request-parsing layer.
    date_param: Annotated[str, Query(alias="date")] = "",
    date_page: int | None = None,
    sort: str = "",
    # `dir` (query key) is kept off the python parameter name so it doesn't
    # shadow the `dir()` builtin anywhere in this function's body — same
    # spirit as the `date`/`date_param` split above.
    sort_dir: Annotated[str, Query(alias="dir")] = "asc",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Validate period parameter
    if period not in ("today", "yesterday", "tomorrow", "earlier"):
        period = "today"

    # Validate ready parameter (second, independent filter — CLAUDE.md section 9, screen 1)
    if ready not in READY_FILTERS:
        ready = "all"

    # Validate source independently from the period/readiness filters.
    if source not in SOURCE_FILTERS:
        source = "all"

    # Validate the optional column sort (queue.html thead, via
    # _sortable_th.html). Absent/invalid `sort` means "no explicit column
    # sort" — the queue keeps its default urgency-based ordering below.
    if sort not in QUEUE_SORT_FIELDS:
        sort = ""
    if sort_dir not in ("asc", "desc"):
        sort_dir = "asc"

    # "Прострочено" KPI shortcut: overdue work can land in either the
    # "yesterday" or "earlier" bucket, so it needs its own cross-period view
    # rather than a period value. Independent of, and takes priority over,
    # the period tabs — clicking any period/source/ready filter link drops it
    # (those links never carry `overdue`).
    show_overdue = overdue == "1"

    # Day-strip filter (sidebar "Дні" group): an explicit, single calendar
    # day chosen from the set of days that actually have order data (see
    # `_known_order_dates` — sourced from `Order.sheet_tab`, so it's always
    # in sync with whatever tabs the Sheet has, no separate lookup needed).
    # Same precedence rule as `show_overdue` above: independent of, and
    # takes priority over, the period bucket for this request; `source`/
    # `ready` stay independent and still apply on top either way. An
    # invalid/unparseable value is silently ignored (falls back to `period`)
    # rather than erroring, same spirit as the period/ready/source fallbacks.
    selected_date = _parse_sheet_tab(date_param)

    # Fetch all orders
    all_orders = db.scalars(select(Order).order_by(Order.id.desc())).all()

    # Define date boundaries
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    # Categorize orders into buckets
    buckets = {"today": [], "yesterday": [], "tomorrow": [], "earlier": []}

    for order in all_orders:
        order_date = _order_date(order)
        if order_date == today:
            buckets["today"].append(order)
        elif order_date == yesterday:
            buckets["yesterday"].append(order)
        elif order_date == tomorrow:
            buckets["tomorrow"].append(order)
        else:
            buckets["earlier"].append(order)

    # Get the filtered list for the current period, every overdue order
    # across all periods when the "Прострочено" KPI shortcut is active, or
    # exactly one calendar day when a day-strip date is selected. `overdue`
    # keeps top priority (unchanged, pre-existing behavior); `date` is the
    # next priority, ahead of the plain period bucket.
    if show_overdue:
        orders = sorted(
            (o for o in all_orders if is_overdue(o.sheet_tab, o.status)),
            key=_queue_sort_key,
        )
    elif selected_date is not None:
        orders = sorted(
            (o for o in all_orders if _order_date(o) == selected_date),
            key=_queue_sort_key,
        )
    else:
        orders = sorted(buckets[period], key=_queue_sort_key)

    # Source chip counts cover the selected period before applying source.
    source_counts = count_by_source(orders)
    orders = filter_by_source(orders, source)

    # Count for all buckets
    counts = {k: len(v) for k, v in buckets.items()}

    attach_export_folder_uris(db, orders)
    attach_job_code_folder_uris(db, orders)

    # Second, independent filter: readiness (has the technician dropped files yet?)
    ready_counts = count_by_readiness(orders)
    orders = filter_by_readiness(orders, ready)

    # Explicit, opt-in column sort (queue.html thead) applied last, on top
    # of whatever period/source/date/ready filtering produced above. With no
    # `sort`, this is a no-op — the default urgency-based _queue_sort_key
    # ordering from earlier is left completely untouched.
    if sort:
        orders = _sort_orders_by_column(orders, sort, sort_dir)

    # Queue table visually separates lab-sheet rows from mail-sourced rows
    # (queue.html: "Лабораторні роботи" / "Роботи з пошти") — mirrors both
    # the real Google Sheet's own convention (lab rows in the main block,
    # mail placeholder rows appended below, see append_mail_placeholder_row)
    # and gives each source its own collapsible section. Splitting the
    # already-filtered-and-sorted `orders` list preserves every filter/sort
    # applied above; each sublist stays correctly ordered within itself.
    orders_lab = [o for o in orders if o.source != "email"]
    orders_email = [o for o in orders if o.source == "email"]

    sync_flash = request.session.pop("sync_flash", None)
    pending_emails = db.scalars(
        select(EmailMessage)
        .where(EmailMessage.status == "нове")
        .options(selectinload(EmailMessage.attachments))
        .order_by(
            EmailMessage.received_at.asc().nullslast(),
            EmailMessage.created_at.asc(),
            EmailMessage.id.asc(),
        )
    ).all()
    attach_email_folder_availability(
        pending_emails,
        _mail_trusted_roots(db),
    )
    attach_email_preview_tokens(pending_emails, _mail_trusted_roots(db), _mail_preview_roots(db))
    pending_mail_count = len(pending_emails)

    # Dashboard header (Варіант B): KPI row (small, hard counts) and peek row
    # (state of the three neighboring screens) — every card is a real link/
    # filter, computed from data already fetched above plus at most one light
    # extra query each, never the heavy export-folder scan or a duplicate of
    # get_stats' full pass.
    overdue_count = sum(1 for o in all_orders if is_overdue(o.sheet_tab, o.status))
    due_today_count = sum(1 for o in buckets["today"] if o.status != "видано")
    clients_without_handout = _handout_pending_client_count(all_orders, today)

    kpis = {
        "overdue": overdue_count,
        "due_today": due_today_count,
        "pending_mail": pending_mail_count,
        "clients_without_handout": clients_without_handout,
    }
    peeks = {
        "handout": _queue_handout_summary(all_orders, today),
        "stats": _queue_week_summary(db, all_orders, today),
        "sync": _queue_sync_summary(db),
    }
    sync_status = _queue_sync_status(db, datetime.now())

    # Day-strip: 7 known dates at a time out of every distinct day that has
    # order data (see `_known_order_dates` / `_date_window` docstrings above
    # for why this is enough to stay in sync with the Sheet with no new
    # sync mechanism).
    known_dates = _known_order_dates(db)
    date_tabs, current_date_page, total_date_pages = _date_window(known_dates, today, date_page)

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "page_title": "Черга робіт",
            "orders": orders,
            "orders_lab": orders_lab,
            "orders_email": orders_email,
            "user": user,
            "statuses": STATUSES,
            "period": period,
            "counts": counts,
            "ready": ready,
            "ready_counts": ready_counts,
            "source": source,
            "source_counts": source_counts,
            "show_overdue": show_overdue,
            "kpis": kpis,
            "peeks": peeks,
            "sync_status": sync_status,
            "has_any_orders": bool(all_orders),
            "sheets_configured": _sheets_configured(db),
            "sync_flash": sync_flash,
            "pending_emails": pending_emails,
            "pending_mail_count": pending_mail_count,
            "selected_date": selected_date,
            "date_tabs": date_tabs,
            "date_page": current_date_page,
            "total_date_pages": total_date_pages,
            "sort": sort,
            "sort_dir": sort_dir,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def get_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    results = []
    query_term = (q or "").strip()

    truncated = False
    if query_term:
        # Search in client_name, work_order_no, job_code, sum3d_id
        # Case-insensitive substring matching across all four fields
        all_orders = db.scalars(select(Order)).all()
        query_lower = query_term.lower()

        for order in all_orders:
            # Check if query appears in any of the four fields (case-insensitive)
            if any(
                (field and query_lower in (field or "").lower())
                for field in [
                    order.client_name,
                    order.work_order_no,
                    order.job_code,
                    order.sum3d_id,
                ]
            ):
                results.append(order)

        # Cap results at 100 and flag if truncated
        if len(results) > 100:
            truncated = True
            results = results[:100]

        # Attach folder info for display
        attach_export_folder_uris(db, results)
        attach_job_code_folder_uris(db, results)

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": query_term,
            "results": results,
            "truncated": truncated,
            "user": user,
            "statuses": STATUSES,
        },
    )


@app.post("/sheets/sync")
def sync_sheets(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    try:
        summary = sync_google_sheets(db)
    except SheetSyncError as exc:
        request.session["sync_flash"] = {"kind": "error", "message": str(exc)}
    else:
        request.session["sync_flash"] = {
            "kind": "success",
            "message": _sync_summary_message(summary),
        }
    return RedirectResponse("/", status_code=303)


@app.post("/orders/{order_id}/sum3d-id", response_class=HTMLResponse)
async def set_sum3d_id(
    request: Request,
    order_id: int,
    sum3d_id: str = Form(...),
    db: Session = Depends(get_db),
):
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    order.sum3d_id = sum3d_id.strip() or None
    sync_error = _write_sheet_fields(db, order, {"sum3d_id"})
    db.commit()
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    return templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES, "sync_error": sync_error}
    )


@app.post("/orders/{order_id}/status", response_class=HTMLResponse)
async def set_status(
    request: Request,
    order_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="невідомий статус")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    order.status = status
    sheet_fields = apply_status_markers(
        order,
        status,
        actor=user.full_name or user.username,
    )
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=status, actor=user.username)
    )
    sync_error = _write_sheet_fields(db, order, sheet_fields)
    db.commit()
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    return templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES, "sync_error": sync_error}
    )


@app.post("/orders/{order_id}/comments")
async def add_order_comment(
    request: Request,
    order_id: int,
    text: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    clean_text = text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="коментар не може бути порожнім")

    now = datetime.now()
    author = user.full_name or user.username
    comment = Comment(
        order_id=order.id,
        source="portal",
        author=author,
        text=clean_text,
    )
    db.add(comment)

    sync_error = None
    if order.sheet_tab and order.source == "lab":
        line = f"[{now:%d.%m.%Y %H:%M} · {author}] {clean_text}"
        try:
            worksheet = get_worksheet_by_name(open_spreadsheet(db=db), order.sheet_tab)
            if worksheet is None:
                raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
            order.cam_comment = append_order_comment(worksheet, order, line)
            comment.synced_at = now
            db.add(
                SyncLog(
                    direction="db_to_sheet",
                    sheet_tab=order.sheet_tab,
                    status="ok",
                    message=f"order {order.id}: comment",
                )
            )
        except Exception as exc:
            sync_error = str(exc)
            db.add(
                SyncLog(
                    direction="db_to_sheet",
                    sheet_tab=order.sheet_tab,
                    status="error",
                    message=f"order {order.id}: comment: {exc}",
                )
            )

    db.commit()
    target = f"/orders/{order.id}"
    if sync_error:
        message = "Коментар збережено локально, але не записано в таблицю: " + sync_error
        target += f"?error={quote(message)}"
    return RedirectResponse(target, status_code=303)


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def get_order_detail(
    request: Request,
    order_id: int,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "order": order,
            "user": user,
            "error": error,
        },
    )


@app.get("/handout", response_class=HTMLResponse)
def get_handout(request: Request, source: str = "all", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    if source not in HANDOUT_SOURCE_FILTERS:
        source = "all"

    today = date.today()
    candidates = db.scalars(
        select(Order).where(Order.client_name.is_not(None), Order.status != "видано")
    ).all()

    groups: dict[str, list[Order]] = {}
    for order in candidates:
        order_date = _parse_sheet_tab(order.sheet_tab)
        if order_date is not None and order_date >= today:
            continue
        groups.setdefault(order.client_name, []).append(order)

    entries = scan_export_folder(Path(get_export_folder_path(db)))
    folder_names = sorted({e.client_folder_name for e in entries})
    aliases = {
        a.sheet_name: a.export_folder_name
        for a in db.scalars(select(ClientNameAlias).where(ClientNameAlias.confirmed.is_(True))).all()
    }

    client_groups = []
    for client_name, group_orders in groups.items():
        match = match_client_name(client_name, folder_names, aliases)
        export_entries = (
            [e for e in entries if e.client_folder_name == match.matched_folder_name]
            if match.matched_folder_name
            else []
        )
        for entry in export_entries:
            entry.folder_uri = folder_to_file_uri(entry.folder_path)
            entry.preview_token = build_preview_token(
                entry.folder_path, {"export": get_export_folder_path(db)}
            )
        all_found = all(o.status in ("знайдено при видачі", "видано") for o in group_orders)
        client_groups.append(
            {
                "client_name": client_name,
                "orders": group_orders,
                "match": match,
                "export_entries": export_entries,
                "all_found": all_found,
            }
        )

    source_counts = count_client_groups_by_source(client_groups)
    client_groups = filter_client_groups_by_source(client_groups, source)

    return templates.TemplateResponse(
        request,
        "handout.html",
        {
            "page_title": "Ранкова видача",
            "user": user,
            "client_groups": client_groups,
            "source": source,
            "source_counts": source_counts,
        },
    )


@app.post("/orders/{order_id}/mark-found")
async def mark_found(request: Request, order_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    order.status = "знайдено при видачі"
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username)
    )
    db.commit()

    return RedirectResponse("/handout", status_code=303)


@app.post("/handout/confirm-alias")
async def confirm_alias(
    request: Request,
    sheet_name: str = Form(...),
    export_folder_name: str = Form(...),
    db: Session = Depends(get_db),
):
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    existing = db.scalar(select(ClientNameAlias).where(ClientNameAlias.sheet_name == sheet_name))
    if existing is not None:
        existing.export_folder_name = export_folder_name
        existing.confirmed = True
        existing.confirmed_at = datetime.now()
    else:
        db.add(
            ClientNameAlias(
                sheet_name=sheet_name,
                export_folder_name=export_folder_name,
                confirmed=True,
                confirmed_at=datetime.now(),
            )
        )
    db.commit()

    return RedirectResponse("/handout", status_code=303)


@app.get("/stl-preview/{token}")
def list_stl_preview_files(request: Request, token: str, db: Session = Depends(get_db)):
    """Lists `.stl` filenames for the hover preview popup (app/static/js/stl-preview.js).

    `token` is opaque and never a raw filesystem path — see app/stl_preview.py
    for why. An unresolvable/tampered token degrades to 404, same as any
    other "folder not found" case in this app; it never falls back to
    trusting the token's contents directly.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    folder = resolve_preview_folder(db, token)
    if folder is None:
        raise HTTPException(status_code=404, detail="папку не знайдено")

    return {"files": list_stl_files(folder)}


@app.get("/stl-preview/{token}/{filename}")
def get_stl_preview_file(
    request: Request, token: str, filename: str, db: Session = Depends(get_db)
):
    """Streams one `.stl` file's bytes for the hover preview popup.

    `folder` is re-derived from `token` on every call (never cached from the
    list call above) and `filename` is re-validated against that folder by
    `resolve_stl_file` — no path separators, `.stl` extension only, must
    resolve to an existing regular file directly inside `folder`.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    folder = resolve_preview_folder(db, token)
    if folder is None:
        raise HTTPException(status_code=404, detail="папку не знайдено")

    file_path = resolve_stl_file(folder, filename)
    if file_path is None:
        raise HTTPException(status_code=404, detail="файл не знайдено")

    return FileResponse(file_path, media_type="model/stl")


@app.get("/stats", response_class=HTMLResponse)
def get_stats(request: Request, period: str = "week", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    if period not in ("today", "week", "month", "all"):
        period = "week"

    today = date.today()
    period_start = {
        "today": today,
        "week": today - timedelta(days=6),
        "month": today - timedelta(days=29),
        "all": None,
    }[period]

    all_orders = db.scalars(select(Order).options(selectinload(Order.status_events))).all()

    period_orders = []
    for order in all_orders:
        if period == "all":
            period_orders.append(order)
            continue
        order_date = _order_date(order)
        if period_start <= order_date <= today:
            period_orders.append(order)

    order_count = len(period_orders)
    quantity_sum = sum(
        qty for qty in (parse_int_safe(order.quantity) for order in period_orders) if qty is not None
    )

    rework_records = db.scalars(select(ReworkRecord)).all()
    rework_groups = summarize_rework_by_blame(rework_records)

    avg_hours = average_new_to_milled_hours(period_orders)

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "page_title": "Статистика",
            "user": user,
            "period": period,
            "order_count": order_count,
            "quantity_sum": quantity_sum,
            "rework_groups": rework_groups,
            "avg_hours": avg_hours,
        },
    )


@app.get("/clients", response_class=HTMLResponse)
def get_clients(request: Request, db: Session = Depends(get_db)):
    """Screen: list of client profiles (CLAUDE.md — not admin-gated, any
    operator can view/manage clients, same as /stats).

    Order counts are computed via app.client_profile.find_matching_orders
    against every order that has a client_name — see that module's
    docstring for why this is a read-time fuzzy match rather than a stored
    FK, and why doing this over the full Order table on every request is
    fine at this project's scale.
    """
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    clients = db.scalars(select(Client).order_by(Client.canonical_name)).all()
    named_orders = db.scalars(select(Order).where(Order.client_name.isnot(None))).all()

    client_rows = [
        {"client": client, "order_count": len(find_matching_orders(client.canonical_name, named_orders))}
        for client in clients
    ]

    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "user": user,
            "client_rows": client_rows,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved") is not None,
        },
    )


@app.post("/clients", response_class=HTMLResponse)
def create_client(
    request: Request,
    canonical_name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    name = canonical_name.strip()
    if not name:
        return RedirectResponse("/clients?error=ім'я+клієнта+обов'язкове", status_code=303)

    client = Client(
        canonical_name=name,
        phone=phone.strip() or None,
        email=email.strip() or None,
        notes=notes.strip() or None,
    )
    db.add(client)
    db.commit()

    return RedirectResponse(f"/clients/{client.id}", status_code=303)


@app.get("/clients/{client_id}", response_class=HTMLResponse)
def get_client_detail(request: Request, client_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="клієнта не знайдено")

    named_orders = db.scalars(select(Order).where(Order.client_name.isnot(None))).all()
    matched_orders = find_matching_orders(client.canonical_name, named_orders)
    summary = summarize_client_orders(matched_orders)

    return templates.TemplateResponse(
        request,
        "client_detail.html",
        {
            "user": user,
            "client": client,
            "summary": summary,
            "saved": request.query_params.get("saved") is not None,
        },
    )


@app.post("/clients/{client_id}", response_class=HTMLResponse)
def update_client(
    request: Request,
    client_id: int,
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="клієнта не знайдено")

    client.phone = phone.strip() or None
    client.email = email.strip() or None
    client.notes = notes.strip() or None
    db.commit()

    return RedirectResponse(f"/clients/{client_id}?saved=1", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def get_settings(
    request: Request,
    saved: str | None = None,
    welcome: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    # Full page is reachable by any operator now — only the Шляхи папок card
    # (below, gated per-field via operator_editable) and the two path
    # HTMX checks are actually operator-facing; settings.html hides every
    # other card behind {% if user.role == 'адмін' %}, and the POST handler
    # enforces the same boundary server-side regardless of what the DOM shows.
    values = get_all_settings(db)
    operators = db.scalars(select(User).order_by(User.created_at)).all() if user.role == "адмін" else []
    settings_flash = request.session.pop("settings_flash", None)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "fields": SETTING_FIELDS,
            "values": values,
            "user": user,
            "saved": saved is not None or (settings_flash and settings_flash["kind"] == "success"),
            "saved_message": (
                settings_flash["message"]
                if settings_flash and settings_flash["kind"] == "success" and settings_flash.get("message")
                else None
            ),
            "welcome": welcome is not None,
            "sheets_configured": _sheets_configured(db),
            "imap_configured": _imap_configured(db),
            "operators": operators,
            "error": error or (
                settings_flash["message"]
                if settings_flash and settings_flash["kind"] == "error"
                else None
            ),
        },
    )


@app.post("/settings", response_class=HTMLResponse)
async def post_settings(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    is_admin = user.role == "адмін"

    form = await request.form()
    action = form.get("action", "save")
    # Field-level enforcement, not just a hidden card: an operator's rendered
    # form only contains the two path inputs, but a hand-crafted POST could
    # still include google_sheet_id/imap_password — silently drop anything
    # outside OPERATOR_EDITABLE_KEYS for a non-admin rather than trusting the
    # DOM to have hidden it.
    for field in SETTING_FIELDS:
        if not is_admin and field.key not in OPERATOR_EDITABLE_KEYS:
            continue
        value = form.get(field.key, "").strip()
        if value:
            set_setting(db, field.key, value)
    db.commit()

    if action == "save_and_sync" and not is_admin:
        action = "save"

    if action == "save_and_sync":
        try:
            summary = sync_google_sheets(db)
        except SheetSyncError as exc:
            request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
            return RedirectResponse("/settings?welcome=1", status_code=303)
        request.session["sync_flash"] = {
            "kind": "success",
            "message": _sync_summary_message(summary),
        }
        return RedirectResponse("/", status_code=303)

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/check-path", response_class=HTMLResponse)
def check_settings_path(
    request: Request,
    kind: str = Form(""),
    export_folder_path: str | None = Form(None),
    technician_files_path: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Live, per-machine reachability/writability check for A3 (CLAUDE.md
    section 7's "мобільним під різні ситуації та ПК"). HTMX-triggered on
    blur/typing from settings.html — see the two `hx-post`-wired path inputs
    there. Both real field names are accepted and `kind` just selects which
    one this particular request means; the response never touches the DB.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Operator-editable (see OPERATOR_EDITABLE_KEYS) — both fields this check
    # serves are filesystem paths, not credentials, so any logged-in user may
    # probe them, same as they may now save them.
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    raw_path = export_folder_path if kind == "export" else technician_files_path
    result = _check_path_status(raw_path or "")

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )


@app.post("/settings/test-imap", response_class=HTMLResponse)
def test_imap_connection(request: Request, db: Session = Depends(get_db)):
    """A4: real IMAP LOGIN-only probe against whatever is CURRENTLY SAVED in
    the DB (not unsaved form values — save first, same as "Зберегти й
    синхронізувати" already works for Google Sheets). Never fetches messages
    and never leaks raw IMAP server error text into the UI, matching the
    safe-error discipline in app/mail_sync_service.py::_safe_error.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    login = get_imap_login(db)
    password = get_imap_password(db)
    if not login or not password:
        result = {
            "state": "error",
            "message": "Спочатку збережіть логін і пароль пошти",
        }
    else:
        try:
            with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password):
                pass
        except Exception:
            logger.warning("IMAP test connection failed for login %s", login)
            result = {
                "state": "error",
                "message": "Не вдалося підключитися. Перевірте логін, пароль для програм та інтернет",
            }
        else:
            result = {"state": "success", "message": "З'єднання з поштою успішне"}

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )


@app.post("/settings/backup/export")
def export_backup(
    request: Request,
    backup_password: str = Form(...),
    backup_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    """CLAUDE.md section 14: "backup для перенесення ПК" — a full, portable
    snapshot (every table, every secret) re-encrypted under a password the
    admin sets here and now, independent of this machine's DPAPI key. See
    app/backup.py for why that independence is the whole point.
    """
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if len(backup_password) < 8:
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Пароль резервної копії має містити щонайменше 8 символів",
        }
        return RedirectResponse("/settings", status_code=303)
    if backup_password != backup_password_confirm:
        request.session["settings_flash"] = {"kind": "error", "message": "Паролі не збігаються"}
        return RedirectResponse("/settings", status_code=303)

    content = create_backup(db, backup_password)
    filename = f"orderdesk-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/settings/backup/import", response_class=HTMLResponse)
async def import_backup(
    request: Request,
    backup_password: str = Form(...),
    confirm_replace: str = Form(""),
    backup_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Restore is destructive by design (app/backup.py) — every current
    order, client, user, and secret gets replaced with what's in the file,
    not merged. `confirm_replace` is a required checkbox in settings.html so
    that's a deliberate, informed click, not a misplaced file picker.
    """
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if confirm_replace != "on":
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Підтвердьте, що поточні дані буде замінено",
        }
        return RedirectResponse("/settings", status_code=303)

    raw = await backup_file.read()
    try:
        counts = restore_backup(db, raw, backup_password)
    except BackupPasswordError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings", status_code=303)
    except BackupFormatError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings", status_code=303)

    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Відновлено: {counts.get('orders', 0)} робіт, {counts.get('clients', 0)} клієнтів, "
        f"{counts.get('users', 0)} операторів. Увійдіть повторно, якщо змінилися облікові дані.",
    }
    return RedirectResponse("/settings", status_code=303)


def _install_update_in_background(release) -> None:
    """Runs on its own daemon thread — download+verify+silent-install can
    take a while (network download, then Inno Setup itself), and the HTTP
    response to the admin's click must not block on any of that."""
    try:
        installer_path = download_and_verify(release)
        launch_silent_install(installer_path)
    except Exception:
        logger.exception("Background update install failed for release %s", release.version)


@app.post("/settings/update/install")
def install_update(request: Request, db: Session = Depends(get_db)):
    """Admin-triggered install of the update already found by the
    background checker (app/update_check.py). Downloads, verifies the
    checksum, and launches the silent installer + relaunch watchdog on a
    background thread — see _install_update_in_background above and
    launch_silent_install's docstring for the skipifsilent workaround.
    """
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    release = get_known_update()
    if release is None:
        request.session["settings_flash"] = {"kind": "error", "message": "Оновлень немає"}
        return RedirectResponse("/settings", status_code=303)

    try:
        Thread(
            target=_install_update_in_background,
            args=(release,),
            name="order-desk-update-install",
            daemon=True,
        ).start()
    except Exception:
        logger.exception("Failed to start update install thread for release %s", release.version)
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Не вдалося запустити встановлення оновлення",
        }
        return RedirectResponse("/settings", status_code=303)

    request.session["settings_flash"] = {
        "kind": "success",
        "message": "Оновлення встановлюється, застосунок автоматично перезапуститься за кілька секунд",
    }
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users", response_class=HTMLResponse)
async def create_operator(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()
    full_name = form.get("full_name", "").strip() or None
    role = form.get("role", "оператор").strip() or "оператор"

    if not username or not password:
        return RedirectResponse("/settings?error=логін+і+пароль+обов'язкові", status_code=303)

    existing = db.scalar(select(User).where(User.username == username))
    if existing is not None:
        return RedirectResponse("/settings?error=такий+логін+вже+існує", status_code=303)

    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name,
            role=role,
        )
    )
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/users/{user_id}/toggle-active", response_class=HTMLResponse)
async def toggle_operator_active(
    request: Request, user_id: int, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")
    if target.id == user.id:
        return RedirectResponse(
            "/settings?error=не+можна+деактивувати+власний+обліковий+запис", status_code=303
        )

    target.is_active = not target.is_active
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/users/{user_id}/reset-password", response_class=HTMLResponse)
async def reset_operator_password(
    request: Request, user_id: int, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")

    form = await request.form()
    new_password = form.get("new_password", "").strip()
    if not new_password:
        return RedirectResponse("/settings?error=введіть+новий+пароль", status_code=303)

    target.password_hash = hash_password(new_password)
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/mail", response_class=HTMLResponse)
def get_mail(
    request: Request,
    db: Session = Depends(get_db),
    synced: int | None = None,
    error: str | None = None,
    service: str = "all",
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Validate independently — an unknown/stale value degrades to "all"
    # (show everything) rather than erroring, same pattern as the queue
    # screen's source/ready filters.
    if service not in SERVICE_TYPE_FILTERS:
        service = "all"

    emails = db.scalars(
        select(EmailMessage)
        .where(EmailMessage.status == "нове")
        .options(selectinload(EmailMessage.attachments))
        .order_by(
            EmailMessage.received_at.desc().nullslast(),
            EmailMessage.created_at.desc()
        )
    ).all()

    # Counts reflect the full "нове" list, before the visual service-type
    # filter is applied, so the chip badges show totals regardless of which
    # chip is currently active.
    service_counts = count_by_service_type(emails)
    emails = filter_emails_by_service_type(emails, service)
    attach_email_preview_tokens(emails, _mail_trusted_roots(db), _mail_preview_roots(db))

    return templates.TemplateResponse(
        request,
        "mail_triage.html",
        {
            "page_title": "Нові з пошти",
            "emails": emails,
            "user": user,
            "synced": synced,
            "error": error,
            "service": service,
            "service_counts": service_counts,
        },
    )


@app.post("/mail/sync")
def sync_mail(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    try:
        count = sync_mailbox(db, Path(MAIL_ATTACHMENTS_PATH), trigger="manual")
    except (MailSyncBusyError, MailSyncError) as exc:
        return RedirectResponse(f"/mail?error={quote(str(exc))}", status_code=303)

    return RedirectResponse(f"/mail?synced={count}", status_code=303)


@app.get("/mail/{email_id}", response_class=HTMLResponse)
def get_mail_detail(
    request: Request,
    email_id: int,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    attach_email_preview_tokens([email], _mail_trusted_roots(db), _mail_preview_roots(db))

    return templates.TemplateResponse(
        request,
        "mail_detail.html",
        {"email": email, "user": user, "error": error},
    )


@app.post("/mail/{email_id}/open-folder", status_code=204)
def open_mail_folder(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    email = db.scalar(
        select(EmailMessage)
        .where(EmailMessage.id == email_id)
        .options(selectinload(EmailMessage.attachments))
    )
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    folder = resolve_email_attachment_folder(
        email.attachments,
        _mail_trusted_roots(db),
    )
    if folder is None:
        raise HTTPException(status_code=404, detail="папку вкладень не знайдено")

    try:
        _open_folder_in_explorer(folder)
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="відкриття папки підтримується лише у Windows")
    except OSError:
        logger.exception("Could not open attachment folder for email %s", email_id)
        raise HTTPException(status_code=500, detail="не вдалося відкрити папку")
    return Response(status_code=204)


@app.post("/mail/{email_id}/accept", response_class=HTMLResponse)
async def accept_email(
    request: Request,
    email_id: int,
    client_name: str = Form(...),
    material_color: str = Form(""),
    kind: str = Form(""),
    quantity: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    if email.status != "нове":
        raise HTTPException(status_code=409, detail="лист уже оброблено")
    if email.attachments_status == "pending":
        # Attachments are still downloading (two-phase fetch, see
        # app.mail_reader.fetch_new_emails). Accepting now would create an
        # order with zero attachments, flip email.status away from "нове"
        # (blocking any later retry via the status guard above), and orphan
        # the files phase 2 saves afterward — there's no code path left that
        # would ever move them into export. Refuse instead of losing files.
        return RedirectResponse(
            f"/mail/{email.id}?error={quote('Вкладення ще завантажуються, зачекайте і спробуйте ще раз')}",
            status_code=303,
        )

    new_order = Order(
        source="email",
        # Real наряд identifier from the sheet — email orders never get one,
        # but sheet_tab is set below to the same "%d.%m.%y" shape table tabs
        # use, so period tabs, is_overdue() and folder lookups treat a priced
        # mail order exactly like one entered from the sheet (CLAUDE.md: an
        # operator wants to find yesterday's mail-sourced job the same way
        # they'd find a table one). row_number stays None on purpose — that's
        # the real signal (source == "lab" too) that stops sheet write-back.
        sheet_tab=date.today().strftime("%d.%m.%y"),
        row_number=None,
        client_name=client_name.strip() or None,
        material_color=material_color.strip() or None,
        kind=kind.strip() or None,
        quantity=quantity.strip() or None,
        status="нове",
    )
    db.add(new_order)
    db.flush()

    email.order_id = new_order.id
    email.status = "прийнято"
    db.add(
        StatusEvent(order_id=new_order.id, operator_id=user.id, status="нове", actor=user.username)
    )

    attachments = [a for a in email.attachments if Path(a.saved_path).exists()]
    if attachments:
        try:
            new_paths = save_attachments_to_export(
                Path(get_export_folder_path(db)),
                new_order.client_name or "",
                new_order.material_color or "",
                [Path(a.saved_path) for a in attachments],
            )
            for attachment, new_path in zip(attachments, new_paths):
                attachment.saved_path = str(new_path)
            db.add(SyncLog(direction="mail_to_export", status="ok", message=f"email {email.id}: {len(attachments)} файл(ів)"))
        except (OSError, ValueError) as exc:
            db.rollback()
            return RedirectResponse(
                f"/mail/{email.id}?error={quote('Не вдалося зберегти вкладення: ' + str(exc))}",
                status_code=303,
            )

    # Mirrors the pricing placeholder line operators already write into the
    # shared sheet by hand for phone/email orders (CLAUDE.md section 2:
    # client name in "Вид роботи", quantity in "Кількість", наряд left
    # blank until priced). Independent of the attachment move above and
    # never allowed to block acceptance: a missing today's tab, a network
    # hiccup, or any other failure is just logged to SyncLog so the
    # operator isn't stuck on a 500 for a convenience write-back.
    try:
        worksheet = get_worksheet_by_name(open_spreadsheet(db=db), new_order.sheet_tab)
        if worksheet is None or worksheet.title != new_order.sheet_tab:
            db.add(
                SyncLog(
                    direction="mail_to_sheet",
                    sheet_tab=new_order.sheet_tab,
                    status="error",
                    message=(
                        f"email {email.id}: вкладку '{new_order.sheet_tab}' не знайдено, "
                        "рядок-нотатку не записано"
                    ),
                )
            )
        else:
            note_row = append_mail_placeholder_row(
                worksheet,
                new_order.client_name or "",
                new_order.quantity or "",
                new_order.material_color or "",
            )
            db.add(
                SyncLog(
                    direction="mail_to_sheet",
                    sheet_tab=new_order.sheet_tab,
                    status="ok",
                    message=f"email {email.id}: рядок-нотатка записана в рядок {note_row}",
                )
            )
    except Exception as exc:
        db.add(
            SyncLog(
                direction="mail_to_sheet",
                sheet_tab=new_order.sheet_tab,
                status="error",
                message=f"email {email.id}: не вдалося записати рядок-нотатку: {exc}",
            )
        )

    db.commit()

    return RedirectResponse("/?source=email", status_code=303)


@app.post("/mail/{email_id}/reject")
async def reject_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    email.status = "відхилено"
    db.commit()

    return RedirectResponse("/mail", status_code=303)
