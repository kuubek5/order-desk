from contextlib import asynccontextmanager
import calendar
from datetime import date, datetime, timedelta, timezone
import ipaddress
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
import uuid
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Form, HTTPException
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
from app.client_matcher import match_client_name
from app.config import MAIL_ATTACHMENTS_PATH, SESSION_SECRET_KEY
from app.db import Base, SessionLocal, engine
from app.export_scanner import scan_export_folder
from app.mail_export import save_attachments_to_export
from app.mail_reader import IMAP_HOST, IMAP_TIMEOUT_SECONDS
from app.mail_sync_service import MailSyncBusyError, MailSyncError, sync_mail_background, sync_mailbox
from app.material_class import material_color_css_class
from app.models import Attachment, ClientNameAlias, Comment, EmailMessage, Order, ReworkRecord, StatusEvent, SyncLog, User
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

try:
    BUSINESS_TIMEZONE = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:  # Windows Python may not bundle the IANA tz database.
    BUSINESS_TIMEZONE = None


_FIRST_ADMIN_LOCK = Lock()
logger = logging.getLogger(__name__)
MAIL_SYNC_INTERVAL_SECONDS = 2 * 60
MAIL_SYNC_INITIAL_DELAY_SECONDS = 10
SHEET_SYNC_INTERVAL_SECONDS = 2 * 60
SHEET_SYNC_INITIAL_DELAY_SECONDS = 10


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


def _mail_sync_worker(stop_event: Event) -> None:
    """Poll IMAP without occupying the web request loop or delaying shutdown."""
    if stop_event.wait(MAIL_SYNC_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                if _imap_configured(db):
                    sync_mail_background(db, Path(MAIL_ATTACHMENTS_PATH))
        except MailSyncBusyError:
            pass
        except MailSyncError as exc:
            logger.warning("Background mail sync failed: %s", exc)
        except Exception:
            logger.exception("Unexpected background mail sync failure")

        stop_event.wait(MAIL_SYNC_INTERVAL_SECONDS)


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
                if _sheets_configured(db):
                    sync_sheets_background(db)
        except SheetSyncBusyError:
            pass
        except SheetSyncError as exc:
            logger.warning("Background sheet sync failed: %s", exc)
        except Exception:
            logger.exception("Unexpected background sheet sync failure")

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
    try:
        yield
    finally:
        mail_stop_event.set()
        mail_thread.join(timeout=1)
        sheet_stop_event.set()
        sheet_thread.join(timeout=1)


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

templates = Jinja2Templates(directory=str(resource_path("app/templates")))
templates.env.globals["is_overdue"] = is_overdue
templates.env.globals["material_color_css_class"] = material_color_css_class

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


@app.get("/", response_class=HTMLResponse)
def get_queue(
    request: Request,
    period: str = "today",
    ready: str = "all",
    source: str = "all",
    overdue: str = "0",
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

    # "Прострочено" KPI shortcut: overdue work can land in either the
    # "yesterday" or "earlier" bucket, so it needs its own cross-period view
    # rather than a period value. Independent of, and takes priority over,
    # the period tabs — clicking any period/source/ready filter link drops it
    # (those links never carry `overdue`).
    show_overdue = overdue == "1"

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

    # Get the filtered list for the current period, or every overdue order
    # across all periods when the "Прострочено" KPI shortcut is active.
    if show_overdue:
        orders = sorted(
            (o for o in all_orders if is_overdue(o.sheet_tab, o.status)),
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

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "page_title": "Черга робіт",
            "orders": orders,
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
            "has_any_orders": bool(all_orders),
            "sheets_configured": _sheets_configured(db),
            "sync_flash": sync_flash,
            "pending_emails": pending_emails,
            "pending_mail_count": pending_mail_count,
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
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    values = get_all_settings(db)
    operators = db.scalars(select(User).order_by(User.created_at)).all()
    settings_flash = request.session.pop("settings_flash", None)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "fields": SETTING_FIELDS,
            "values": values,
            "user": user,
            "saved": saved is not None or (settings_flash and settings_flash["kind"] == "success"),
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
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    form = await request.form()
    action = form.get("action", "save")
    for field in SETTING_FIELDS:
        value = form.get(field.key, "").strip()
        if value:
            set_setting(db, field.key, value)
    db.commit()

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
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
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
