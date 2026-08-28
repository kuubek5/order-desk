from collections import defaultdict
from contextlib import asynccontextmanager
import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import ipaddress
import json
import logging
import os
import socket
import ssl
import time
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from time import monotonic
from typing import Annotated, Callable
import uuid
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import gspread
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from imap_tools import MailBox
from sqlalchemy import and_ as sa_and, func, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app.__version__ import VERSION
from app.changelog import load_changelog
from app.auth import hash_password, verify_password
from app.backup import BackupFormatError, BackupPasswordError, create_backup, restore_backup
from app.client_profile import (
    count_matching_orders,
    find_matching_orders,
    index_orders_by_name,
    summarize_client_orders,
)
from app.config import DB_PATH, MAIL_ATTACHMENTS_PATH, SESSION_SECRET_KEY
from app.db import Base, SessionLocal, engine
from app.monthly_backup import ensure_monthly_snapshot, list_snapshots
from app.export_scanner import (
    clear_export_cache,
    list_export_client_names_cached,
)
from app import sync_control
from app import sync_heartbeat
from app.sync_heartbeat import sync_status_pair as _queue_sync_status
from app.sync_control import (
    MAIL_SYNC_INITIAL_DELAY_SECONDS,
    MAIL_SYNC_INTERVAL_SECONDS,
    SHEET_SYNC_HOT_INTERVAL_SECONDS,
    SHEET_SYNC_INITIAL_DELAY_SECONDS,
    SHEET_SYNC_INTERVAL_SECONDS,
    hot_extra_days as _hot_extra_days,
    record_viewed_day as _record_viewed_day,
)
from app.sync_heartbeat import (
    heartbeat_status as _sync_heartbeat_status,
    record_heartbeat as _record_sync_heartbeat,
)
from app.services.config_state import (
    sheets_access_error_message as _sheets_access_error_message,
    imap_configured as _imap_configured,
    mail_preview_roots as _mail_preview_roots,
    mail_trusted_roots as _mail_trusted_roots,
    sheets_configured as _sheets_configured,
)
from app.sync_control import SYNC_SPEED_PRESETS, get_sync_speed
from app.license import get_license_status, get_machine_id, verify_license_key
from app.mail_export import (
    list_client_folders,
    preview_export_target,
    restore_attachments_to_spool,
    save_attachments_to_export,
)
from app.mail_parser import material_candidates
from app.link_attachments import (
    LinkAttachment,
    LinkDownloadError,
    download_link,
    extract_download_links,
)
from app.archive_extract import is_archive
from app.mail_reader import IMAP_HOST, IMAP_TIMEOUT_SECONDS, extract_archive_attachments
from app.mail_reader import download_attachments_now
from app.mail_sync_service import (
    run_sync_owned_session as _run_mail_sync_owned_session,
    MailSyncBusyError,
    MailSyncError,
    MailSyncTimeoutError,
    sync_mail_background,
    sync_mailbox,
)
from app.material_class import (
    strip_material_word,
    material_badge,
    material_color_css_class,
    split_material_color,
)
from app.material_catalog import (
    MaterialCatalogError,
    add_alias,
    add_material,
    backfill_orders,
    delete_alias,
    ensure_seeded,
    list_materials,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
    unresolved_order_count,
)
from app.mail_filters import apply_rule_retroactively
from app.mail_spool import analyze_spool, prune_spool
from app.models import ActionLog, AppSetting, Attachment, ClientNameAlias, ClientSenderMemory, Comment, EmailMessage, MailFilterCategory, MailFilterRule, Order, ReworkRecord, StatusEvent, SyncLog, User
from app.sender_memory import list_sender_memories, lookup_sender, remember_sender
from app.order_folder import (
    attach_email_folder_availability,
    attach_email_preview_tokens,
    attach_export_folder_uris,
    attach_job_code_folder_uris,
    resolve_email_attachment_folder,
)
from app.queue_filters import (
    READY_FILTERS,
    SERVICE_TYPE_FILTERS,
    SOURCE_FILTERS,
    count_by_readiness,
    count_by_service_type,
    count_by_source,
    filter_by_readiness,
    filter_by_source,
    filter_emails_by_service_type,
)
from app.runtime import resource_path
from app.services.clients import (
    ensure_client_profiles as _ensure_client_profiles,
    quantity_units as _quantity_units,
)
from app.services.operators import (
    normalize_initial as _normalize_initial,
    validate_initial as _validate_initial,
)
from app.routers.auth import router as auth_router
from app.routers.clients import router as clients_router
from app.routers.handout import router as handout_router
from app.routers.mail import (
    _mail_filter_categories,
    router as mail_router,
)
from app.routers.orders import router as orders_router
from app.routers.queue import router as queue_router
from app.routers.archive import router as archive_router
from app.routers.stats import router as stats_router
from app.routers.stl import router as stl_router
from app.routers.deps import (
    SYNC_PAUSED_MSG as _SYNC_PAUSED_MSG,
    attach_action_toast as _attach_action_toast,
    get_current_user,
    get_db,
    is_loopback_request as _is_loopback_request,
    templates,
    toast_response as _toast_response,
)
from app.services.order_dates import (
    BUSINESS_TIMEZONE,
    order_date as _order_date,
    parse_sheet_tab as _parse_sheet_tab,
    sheet_order_key as _sheet_order_key,
)
from app.services.queue import (
    DATE_STRIP_WINDOW,
    QUEUE_SORT_FIELDS,
    RETENTION_DAYS,
    date_window as _date_window,
    handout_pending_client_count as _handout_pending_client_count,
    known_order_dates as _known_order_dates,
    order_is_archived as _order_is_archived,
    queue_column_sort_value as _queue_column_sort_value,
    queue_handout_summary as _queue_handout_summary,
    queue_sort_key as _queue_sort_key,
    queue_sync_summary as _queue_sync_summary,
    queue_week_summary as _queue_week_summary,
    sort_orders_by_column as _sort_orders_by_column,
)
from app.services.handout import (
    EXPORT_SCAN_WORKERS,
    HANDOUT_ALL_DAYS,
    entries_for_material as _entries_for_material,
    handout_client_matches as _handout_client_matches,
    handout_day_options as _handout_day_options,
    handout_eligible_orders as _handout_eligible_orders,
    handout_not_before as _handout_not_before,
    handout_select_day as _handout_select_day,
    matched_folders as _matched_folders,
    scan_export_for_clients as _scan_export_for_clients,
    scan_export_latest_for_clients as _scan_export_latest_for_clients,
)
from app.services.sheet_writeback import (
    append_comment_background as _append_comment_background,
    append_manual_rows_warm as _append_manual_rows_warm,
    clear_sheet_row_background as _clear_sheet_row_background,
    restore_sheet_row as _restore_sheet_row,
    set_client_row_fill_background as _set_client_row_fill_background,
    sheet_writeback_pool as _sheet_writeback_pool,
    warm_sheet_writeback as _warm_sheet_writeback,
    write_calculated_cell as _write_calculated,
    write_rework_sum3d_fields as _write_rework_sum3d,
    write_sheet_fields as _write_sheet_fields,
    write_sheet_fields_background as _write_sheet_fields_background,
)
from app.services.undo import (
    UNDOABLE_ACTION_TYPES,
    UNDO_WINDOW_SECONDS,
    UndoOutcome,
    log_action,
    perform_redo,
    perform_undo,
)
from app.services.formatting import (
    UK_MONTHS as _UK_MONTHS,
    pluralize_uk as _pluralize_uk,
    relative_time_uk as _relative_time_uk,
    uk_month_label as _uk_month_label,
)
from app.settings_store import (
    OPERATOR_EDITABLE_KEYS,
    SETTING_FIELDS,
    get_all_settings,
    get_export_folder_path,
    get_google_auth_mode,
    get_google_oauth_client_json,
    get_google_oauth_refresh_token,
    get_google_service_account_json,
    get_google_sheet_id,
    get_imap_login,
    get_imap_password,
    get_mail_default_material,
    get_mail_download_all,
    get_service_account_email,
    get_technician_files_path,
    extract_sheet_id,
    DEFAULT_NOTIFY_POSITION,
    DEFAULT_NOTIFY_STYLE,
    NOTIFY_EVENTS,
    get_notify_events,
    get_notify_position,
    get_notify_style,
    set_notify_prefs,
    set_mail_default_material,
    set_mail_download_all,
    set_setting,
)
from app.sheet_sync_service import (
    summary_message as _sync_summary_message,
    SheetSyncBusyError,
    SheetSyncError,
    SheetSyncSummary,
    sync_google_sheets,
    sync_hot_tab,
    sync_sheets_background,
)
from app.sheet_writer import (
    append_mail_placeholder_row,
    apply_status_markers,
    clear_placeholder_row,
)
from app.parser import HEADER_ROWS
from app.google_oauth import OAuthFlowError, parse_client_config, run_authorization_flow
from app.sheets import (
    get_worksheet_by_name,
    measure_sheet_weight,
    latest_worksheet_on_or_before,
    open_spreadsheet,
    reset_sheets_cache,
)
from app.statuses import STATUSES, is_overdue
from app.triage_status import triage_readiness
from app.stats import (
    average_new_to_milled_hours,
    parse_int_safe,
    summarize_by_material,
    summarize_rework_by_blame,
)
from app.stl_preview import resolve_preview_folder
from app.update_check import (
    _update_check_tick,
    _update_check_worker,
    download_and_verify,
    get_known_update,
    launch_silent_install,
)

logger = logging.getLogger(__name__)
def _settings_changed_at(db: Session, keys: tuple[str, ...]) -> dict[str, str]:
    """`AppSetting.updated_at` per key, formatted "12.08.26".

    Answers "а коли ми міняли пароль пошти?" without touching the value: the
    timestamp column is plaintext, only `value_encrypted` is a secret. Absolute
    dates, not "N days ago" — a settings screen is consulted rarely, so the
    calendar date is what the operator can actually cross-reference.
    """
    rows = db.scalars(select(AppSetting).where(AppSetting.key.in_(keys))).all()
    return {
        row.key: row.updated_at.strftime("%d.%m.%y")
        for row in rows
        if row.updated_at is not None
    }


# Автоматизація Провідника Windows винесена в app/platform_windows.py
# (Крок 2 розбиття web.py). Імпортуємо під старими іменами, щоб роути й
# тести (які монкіпатчать web._open_folder_in_explorer) працювали без змін.
from app.platform_windows import (  # noqa: E402
    open_folder_in_explorer as _open_folder_in_explorer,
    _raise_explorer_window,
)


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

    `db` is used only for the configured-gate read; the sync itself runs on
    a session it owns (see _run_mail_sync_owned_session) so a watchdog
    timeout can't leave this per-tick session in the zombie's hands.
    """
    if not _imap_configured(db):
        return
    try:
        _run_mail_sync_owned_session(trigger="background")
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


def _sheet_hot_tick(db: Session) -> None:
    """One fast-lane attempt at the current day's tab (sync_hot_tab). Errors
    are logged but do NOT flip the heartbeat to error: this runs every ~15s,
    and a transient proxy blip here would flap the UI state that the full
    sync (with its retries and audit trail) is the real owner of."""
    if not _sheets_configured(db):
        return
    try:
        summary = sync_hot_tab(db, extra_days=_hot_extra_days())
    except SheetSyncError as exc:
        logger.warning("Hot-tab sheet sync failed: %s", exc)
        return
    if summary is None:
        return  # lock busy (full sync in flight) or today's tab not created yet
    _record_sync_heartbeat("sheet", status="ok")
    if summary.created or summary.updated or summary.deleted:
        logger.info(
            "Hot-tab sync %s: created %d, updated %d, deleted %d",
            summary.tab_names[0],
            summary.created,
            summary.updated,
            summary.deleted,
        )


def _sheet_sync_worker(stop_event: Event) -> None:
    """Poll Google Sheets without occupying the web request loop or delaying
    shutdown — same shape as _mail_sync_worker above. Table rows are entered
    by trusted internal staff (technologists/admins), not free-text clients,
    so unlike email there is no per-row guess to review before it becomes an
    Order: sync_tab already writes directly. Automating the periodic pull
    just removes the "someone has to remember to click Синхронізувати" step.

    Two-speed loop: the expensive full sync (worksheets listing + 3-day
    window) runs every SHEET_SYNC_INTERVAL_SECONDS; in between, the cheap
    hot-tab read of today's tab runs every SHEET_SYNC_HOT_INTERVAL_SECONDS so
    current-day edits land almost live. Both run on THIS one thread, sharing
    its warm per-thread spreadsheet/worksheet cache (app/sheets.py)."""
    if stop_event.wait(SHEET_SYNC_INITIAL_DELAY_SECONDS):
        return

    next_full = 0.0  # first iteration always does a full sync
    while not stop_event.is_set():
        speed = get_sync_speed()  # live: the UI switch changes the next tick
        # Paused: touch the sheet in neither direction. Keep looping (cheaply)
        # so resume takes effect within one tick; force a full sync on resume so
        # the first thing after a pause is a complete re-read of the table.
        if sync_control.is_paused():
            next_full = 0.0
            stop_event.wait(speed["hot"])
            continue
        run_full = monotonic() >= next_full
        try:
            with SessionLocal() as db:
                if run_full:
                    _sheet_sync_tick(db)
                    next_full = monotonic() + speed["full"]
                else:
                    _sheet_hot_tick(db)
        except Exception:
            logger.exception("Unexpected background sheet sync failure")
            _record_sync_heartbeat(
                "sheet", status="error", error_message="Неочікувана помилка синхронізації таблиці"
            )
            if run_full:
                next_full = monotonic() + speed["full"]

        stop_event.wait(speed["hot"])


# Monthly DB snapshot: check this often whether last month's archive exists
# yet (see app/monthly_backup.py for why it's a poll, not a midnight cron —
# the lab PC is off at midnight). 6h means the archive appears within hours of
# the first launch in a new month, and the check is a single cheap file-exists
# the rest of the time.
MONTHLY_BACKUP_INTERVAL_SECONDS = 6 * 60 * 60
MONTHLY_BACKUP_INITIAL_DELAY_SECONDS = 30


def _monthly_backup_tick() -> None:
    """One archive-if-missing attempt. Never raises — a snapshot failure must
    not touch the running app; it just retries on the next tick."""
    try:
        created = ensure_monthly_snapshot(engine, DB_PATH)
        if created is not None:
            logger.info("Monthly backup written: %s", created.name)
    except Exception:
        logger.exception("Monthly DB snapshot failed")


def _monthly_backup_worker(stop_event: Event) -> None:
    """Create the previous month's DB snapshot at the first opportunity after
    the month rolls over, then idle-check every interval — same
    thread/shutdown shape as the sync workers above."""
    if stop_event.wait(MONTHLY_BACKUP_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        _monthly_backup_tick()
        stop_event.wait(MONTHLY_BACKUP_INTERVAL_SECONDS)


EXPORT_WARM_INITIAL_DELAY_SECONDS = 20.0
EXPORT_WARM_INTERVAL_SECONDS = 120.0


def _export_warm_worker(stop_event: Event) -> None:
    """Тримати кеш обходу export теплим, щоб екран видачі відкривався одразу.

    Обхід сховища коштує рівно стільки, скільки коштує — виміряно 27.08.26 на
    бойовій шарі: 33 мс/запис послідовно, 8.5 мс у 16 потоків, а на екрані
    ~2525 записів. Питання лише в тому, ХТО за це платить. Досі платив
    оператор, який відкрив видачу (80с очікування). Тепер платить фон, а
    оператор отримує вже готове.

    Кеш сканера (`app/export_scanner.py`) віддає протухле одразу й оновлює
    його у фоні, тож інтервал більший за TTL — це не зайве очікування, а
    лише вік даних, і для тек із роботами він неістотний.

    Ключі кешу мусять збігатися з тими, що рахує сам екран, — тому обидва
    йдуть через ті самі помічники (`_handout_*`). Інакше прогрів наповнить
    кеш під іншим ключем, і на екрані нічого не зміниться."""
    if stop_event.wait(EXPORT_WARM_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                export_warm_once(db)
        except Exception:  # noqa: BLE001 — фоновий прогрів не валить застосунок
            logger.exception("Фоновий прогрів export не вдався")
        if stop_event.wait(EXPORT_WARM_INTERVAL_SECONDS):
            return


def export_warm_once(db: Session) -> int:
    """Один прохід прогріву. Повертає кількість прогрітих тек."""
    root = Path(get_export_folder_path(db))
    folder_names = list_export_client_names_cached(root)
    if not folder_names:
        return 0
    eligible = _handout_eligible_orders(db, date.today())
    # Гріємо рівно те, що побачить оператор: екран за замовчуванням показує
    # останній день, тож прогрів усіх днів дав би ІНШУ межу за датою — інший
    # ключ кешу — і оператор однаково чекав би.
    default_day = _handout_select_day(_handout_day_options(eligible), "")
    if default_day is not None:
        eligible = [o for o in eligible if _parse_sheet_tab(o.sheet_tab) == default_day]
    not_before = _handout_not_before(eligible)
    client_names = {o.client_name for o in eligible if o.client_name}
    folders = _matched_folders(_handout_client_matches(db, client_names, folder_names))
    started = time.monotonic()
    scanned = _scan_export_for_clients(root, folders, not_before)
    # Той самий запасний шлях, що й на екрані — інакше перший, хто відкриє
    # видачу, платив би за нього сам.
    empty = {name: folder for name, folder in folders.items() if not scanned.get(name)}
    scanned.update(_scan_export_latest_for_clients(root, empty))
    logger.info(
        "Export prewarm: %d тек, %d записів, %.2fс",
        len(folders),
        sum(len(v) for v in scanned.values()),
        time.monotonic() - started,
    )
    return len(folders)


@dataclass
class _BackgroundWorker:
    """Один фоновий daemon-потік і його вимикач. Раніше кожен воркер
    заводився в lifespan вручну п'ятьма однаковими блоками start/stop —
    легко було проґавити один при зупинці. Тепер це список, а не копіпаст:
    видно всі фонові процеси в одному місці, і додати новий (напр. майбутній
    моніторинг печей) — один рядок.

    Логіку самих воркерів НЕ чіпаємо — вона лишається у web.py; це лише
    впорядкування їхнього життєвого циклу (перший, найбезпечніший крок
    розбиття, див. ARCHITECTURE_PLAN.md)."""
    name: str
    target: Callable
    stop_event: Event = field(default_factory=Event)
    thread: Thread | None = None

    def start(self) -> None:
        self.thread = Thread(
            target=self.target, args=(self.stop_event,), name=self.name, daemon=True
        )
        self.thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.environ.get("ORDER_DESK_SCHEMA_MANAGED") != "1":
        Base.metadata.create_all(engine)
    # Seed the material catalog and classify any still-unresolved orders once at
    # boot. Idempotent and cheap; covers both the create_all path (no migration
    # seed) and the first boot after the 0005 upgrade backfills existing rows.
    # Never let it block startup.
    with SessionLocal() as db:
        try:
            ensure_seeded(db)
            backfill_orders(db)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Material catalog seed/backfill at startup failed")

    # Warm the sheet write-back worker's cache (open the spreadsheet once) so the
    # very first operator edit reflects in the sheet in ~3s instead of ~40s. Best
    # effort — skips silently if the sheet isn't configured or is unreachable.
    _sheet_writeback_pool.submit(_warm_sheet_writeback)

    workers = [
        _BackgroundWorker("order-desk-mail-sync", _mail_sync_worker),
        _BackgroundWorker("order-desk-sheet-sync", _sheet_sync_worker),
        _BackgroundWorker("order-desk-update-check", _update_check_worker),
        _BackgroundWorker("order-desk-monthly-backup", _monthly_backup_worker),
        _BackgroundWorker("order-desk-export-warm", _export_warm_worker),
    ]
    for w in workers:
        w.start()
    try:
        yield
    finally:
        for w in workers:
            w.stop()


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
async def no_store_html(request: Request, call_next):
    """Stop the browser caching dynamic HTML pages.

    Static assets already cache-bust via static_ver()'s ?v=<mtime>, but the
    TemplateResponse HTML itself carried no cache header, so after an app
    upgrade a browser could keep serving a stale page (e.g. mail triage with
    the old floating STL popup) until a hard refresh. HTML is per-session and
    changes constantly (queue, mail) — it must never be cached; static files
    (their own content-type, not text/html) are untouched and stay cacheable.
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


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


app.mount("/static", StaticFiles(directory=str(resource_path("app/static"))), name="static")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Process-level probe without exposing configuration or mutating the DB.

    Reports the running build version so support and the update watchdog's
    post-relaunch check can confirm *which* build answered, not merely that
    one did — the version is not a secret and needs no auth here."""
    return {"status": "ok", "version": VERSION}


# Вхід, ліцензія і кабінет оператора живуть в app/routers/auth.py.
app.include_router(auth_router)


# Черга робіт (основний екран) і ручні дії над синхронізацією живуть
# в app/routers/queue.py.
app.include_router(queue_router)


# Паспорт роботи, дії над нею, скасування і журнал живуть
# в app/routers/orders.py.
app.include_router(orders_router)


# Архів («Хроніка») живе в app/routers/archive.py.
app.include_router(archive_router)


# Екран видачі живе в app/routers/handout.py.
app.include_router(handout_router)


# STL-прев'ю живе в app/routers/stl.py. include_router стоїть саме тут, де
# ці роути й були оголошені, — порядок реєстрації в застосунку не змінюється.
app.include_router(stl_router)


# Статистика живе в app/routers/stats.py.
app.include_router(stats_router)


# Екран «Клієнти» живе в app/routers/clients.py.
app.include_router(clients_router)


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

    # Setup-wizard progress (settings.html "Майстер" layout): five steps, a
    # boolean per step for "готово". These flags are read-only derivations of
    # the same get_setting values used everywhere else — nothing here changes
    # how anything is saved or decrypted.
    google_configured = _sheets_configured(db)
    imap_configured = _imap_configured(db)
    paths_set = bool(
        (get_export_folder_path(db) or "").strip()
        and (get_technician_files_path(db) or "").strip()
    )
    # Independent of the `operators` list above, which is empty for non-admins.
    operators_exist = db.scalar(select(func.count()).select_from(User)) > 0
    backup_available = True  # a snapshot can always be created — no prerequisite
    setup_steps_total = 5
    setup_steps_done = sum(
        1
        for done in (
            google_configured,
            operators_exist,
            backup_available,
            imap_configured,
            paths_set,
        )
        if done
    )

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
            "google_configured": google_configured,
            # The address the spreadsheet must be shared with — without it on
            # screen there is no way to know what to paste into Google's Share
            # dialog, which is the whole of "connecting" in service-account mode.
            "service_account_email": get_service_account_email(db),
            # Curated changelog from CHANGELOG.md, rendered in «Про застосунок».
            "changelog": load_changelog(),
            # Popup-notification preferences («Спливаючі сповіщення»).
            "notify_style": get_notify_style(db),
            "notify_position": get_notify_position(db),
            "notify_events": get_notify_events(db),
            "notify_all": NOTIFY_EVENTS,
            "paths_set": paths_set,
            "operators_exist": operators_exist,
            "backup_available": backup_available,
            "monthly_snapshots": [
                {
                    "name": p.name,
                    "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
                }
                for p in list_snapshots(DB_PATH)
            ],
            "setup_steps_done": setup_steps_done,
            "setup_steps_total": setup_steps_total,
            "operators": operators,
            # Background-loop liveness, same source the queue sidebar renders.
            # "Стан системи" is where an operator actually looks for it, and the
            # stale-heartbeat detector (STALE_HEARTBEAT_MULTIPLIER) is the one
            # signal that distinguishes "quiet because idle" from "worker died".
            "sync_status": _queue_sync_status(db, datetime.now()),
            "sync_intervals": {
                "mail": MAIL_SYNC_INTERVAL_SECONDS // 60,
                "sheet": SHEET_SYNC_INTERVAL_SECONDS // 60,
            },
            "changed_at": _settings_changed_at(
                db,
                (
                    "google_sheet_id",
                    "google_service_account_json",
                    "google_oauth_client_json",
                    "imap_login",
                    "imap_password",
                    "export_folder_path",
                    "technician_files_path",
                ),
            ),
            # «Фільтри пошти» section — same shared panel as the filtered tab.
            "filter_rules": db.scalars(
                select(MailFilterRule).order_by(MailFilterRule.id.desc())
            ).all(),
            "filter_categories": _mail_filter_categories(db),
            "filter_category_rows": db.scalars(
                select(MailFilterCategory).order_by(MailFilterCategory.id.asc())
            ).all(),
            "mail_download_all": get_mail_download_all(db),
            "spool_report": (_spool_report := analyze_spool(db, Path(MAIL_ATTACHMENTS_PATH))),
            # "Стан системи" flow map — honest, cheap counts (one scalar each).
            # No export-folder scan here; that's the heavy walk we keep off page load.
            "state_nodes": [
                {
                    "n": db.scalar(
                        select(func.count())
                        .select_from(EmailMessage)
                        .where(
                            EmailMessage.status == "нове",
                            EmailMessage.filter_category.is_(None),
                        )
                    ) or 0,
                    "l": "Пошта",
                    "u": "у тріажі",
                },
                {"n": _spool_report.total_dirs, "l": "Спул", "u": f"{_spool_report.total_mb} МБ"},
                {
                    "n": db.scalar(
                        select(func.count())
                        .select_from(Order)
                        .where(Order.status != "видано", Order.archived_at.is_(None))
                    ) or 0,
                    "l": "Черга",
                    "u": "активні",
                },
                {
                    "n": db.scalar(
                        select(func.count())
                        .select_from(Order)
                        .where(Order.archived_at.is_not(None))
                    ) or 0,
                    "l": "Архів",
                    "u": "робіт",
                },
            ],
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
        if field.key == "google_sheet_id":
            # Operators paste the whole address-bar URL; store the bare id.
            value = extract_sheet_id(value)
        if value:
            set_setting(db, field.key, value)
    db.commit()

    if action == "save_and_sync" and not is_admin:
        action = "save"

    # HTMX save keeps the operator on the section they were editing. The full
    # POST redirected to /settings?saved=1 — no #hash — which under the console
    # layout lands on «Стан системи» instead of the form just saved. Answer 204
    # (nothing to swap; the DOM already shows what was typed) and report the
    # outcome through the app-wide toast channel. Without JS the plain form
    # still posts here and still gets the redirect below.
    hx = request.headers.get("HX-Request") == "true"

    if action == "save_and_sync":
        try:
            summary = sync_google_sheets(db)
        except SheetSyncError as exc:
            if hx:
                return _toast_response("Синхронізація: " + str(exc), kind="error")
            request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
            return RedirectResponse("/settings?welcome=1", status_code=303)
        message = _sync_summary_message(summary)
        if hx:
            return _toast_response(message, kind="success")
        request.session["sync_flash"] = {"kind": "success", "message": message}
        return RedirectResponse("/", status_code=303)

    if hx:
        return _toast_response("Збережено", kind="success")
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


def _imap_error_reason(exc: Exception) -> str:
    """Turn a raw IMAP failure into a specific, operator-actionable Ukrainian
    reason. The lab operator needs to know WHICH problem it is: a rejected
    app-password / disabled IMAP is a completely different fix from "no
    internet". ukr.net's own login-rejection is the common case and its meaning
    ("check the app-password, enable IMAP access") is genuinely useful, so we
    surface that intent rather than a generic "try again". We still don't echo
    the raw server byte-string into the UI — just the classified reason."""
    from imap_tools.errors import MailboxLoginError

    if isinstance(exc, MailboxLoginError):
        return (
            "Пошта ukr.net відхилила вхід. Найімовірніше протермінувався або "
            "змінився пароль для програм, або в скриньці вимкнено IMAP-доступ. "
            "Згенеруйте новий пароль для програм на ukr.net і увімкніть IMAP, "
            "потім вставте пароль тут."
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Пошта imap.ukr.net не відповіла вчасно — перевірте інтернет-з'єднання."
    if isinstance(exc, ssl.SSLError):
        return "Помилка захищеного з'єднання з imap.ukr.net (можливо, заважає проксі/антивірус)."
    if isinstance(exc, (ConnectionError, socket.gaierror, OSError)):
        return "Немає з'єднання з imap.ukr.net — перевірте інтернет або проксі."
    return "Не вдалося підключитися до пошти. Перевірте логін, пароль для програм та інтернет."


def _imap_check_response(
    request: Request,
    result: dict,
    *,
    toast_kind: str,
    toast_message: str | None = None,
) -> HTMLResponse:
    """Render the inline check pill AND attach an HX-Trigger toast so the same
    reason also pops as a notification inside the CRM (see app.js showToast).
    Belt-and-braces: the pill stays next to the field, the toast makes sure the
    operator can't miss a failure even if the field is scrolled out of view."""
    response = templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )
    # The toast payload rides along as an HX-Trigger header so app.js can pop it
    # without a per-route client handler. getattr-guarded so a test that stubs
    # TemplateResponse to return a plain dict isn't forced to fake a headers map.
    headers = getattr(response, "headers", None)
    if headers is not None:
        headers["HX-Trigger"] = json.dumps(
            {"toast": {"message": toast_message or result["message"], "kind": toast_kind}}
        )
    return response


def _probe_imap_login(login: str | None, password: str | None) -> dict:
    """LOGIN-only probe against the given credentials. Returns a check-result
    dict (state + message). Shared by the save route and the manual test button
    so both give the operator the same specific reason."""
    if not login or not password:
        return {"state": "error", "message": "Спочатку задайте логін і пароль пошти"}
    try:
        with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password):
            pass
    except Exception as exc:  # noqa: BLE001 — classified into a safe reason below
        logger.warning(
            "IMAP login probe failed for login %s: %s", login, type(exc).__name__
        )
        return {"state": "error", "message": _imap_error_reason(exc)}
    return {"state": "success", "message": "З'єднання з поштою успішне"}


@app.post("/settings/imap", response_class=HTMLResponse)
async def save_imap_settings(request: Request, db: Session = Depends(get_db)):
    """HTMX save for the IMAP credentials block. Saves whatever changed, then
    immediately probes LOGIN and returns the result inline — so the operator
    never gets a silent full-page reload that scrolls back to the top, and sees
    the real reason (as a toast) when it fails. A JS-off client still falls back
    to the plain <form action="/settings"> full POST."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    form = await request.form()
    login = (form.get("imap_login") or "").strip()
    password = (form.get("imap_password") or "").strip()
    # Empty password means "keep the saved one" — the field renders blank on
    # purpose (placeholder "•••• збережено"), so a save that only edits the login
    # must not wipe the stored password.
    if login:
        set_setting(db, "imap_login", login)
    if password:
        set_setting(db, "imap_password", password)
    db.commit()

    result = _probe_imap_login(get_imap_login(db), get_imap_password(db))
    if result["state"] == "success":
        return _imap_check_response(
            request, result, toast_kind="success", toast_message="Пошту збережено й підключено"
        )
    return _imap_check_response(
        request, result, toast_kind="error", toast_message="Пошта: " + result["message"]
    )


@app.post("/settings/test-imap", response_class=HTMLResponse)
def test_imap_connection(request: Request, db: Session = Depends(get_db)):
    """A4: real IMAP LOGIN-only probe against whatever is CURRENTLY SAVED in
    the DB (not unsaved form values — save first, same as "Зберегти й
    синхронізувати" already works for Google Sheets). Never fetches messages;
    the raw server error is classified into a specific, safe reason
    (_imap_error_reason) rather than echoed verbatim.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    result = _probe_imap_login(get_imap_login(db), get_imap_password(db))
    toast_kind = "success" if result["state"] == "success" else "error"
    toast_message = (
        result["message"]
        if result["state"] == "success"
        else "Пошта: " + result["message"]
    )
    return _imap_check_response(
        request, result, toast_kind=toast_kind, toast_message=toast_message
    )


@app.post("/settings/test-sheets", response_class=HTMLResponse)
def test_sheets_connection(request: Request, db: Session = Depends(get_db)):
    """Read-only Google Sheets access probe for the settings "Майстер" — opens
    the spreadsheet with whatever sheet id / service-account JSON is CURRENTLY
    SAVED (same "save first" contract as test-imap) and confirms the service
    account can actually reach it, without importing any rows. A successful
    "Зберегти й синхронізувати" already proves the same, but this lets the
    admin verify access without mutating the queue. Raw gspread/Google error
    text is never surfaced to the UI, matching the test-imap discipline.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if not _sheets_configured(db):
        result = {
            "state": "error",
            "message": "Спочатку збережіть ID таблиці та JSON-ключ сервісного акаунта",
        }
    else:
        try:
            spreadsheet = open_spreadsheet(db=db)
            # Touch the worksheet list so a permissions/id error surfaces here,
            # not just an object we never actually read from.
            tabs = spreadsheet.worksheets()
        except Exception as exc:
            logger.warning("Google Sheets access test failed")
            result = {"state": "error", "message": _sheets_access_error_message(db, exc)}
        else:
            result = {
                "state": "success",
                "message": f"Доступ підтверджено · {len(tabs)} вкладок",
            }

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )


def _require_settings_admin(request: Request, db: Session):
    """Admin + loopback gate shared by the settings mutation routes. Returns the
    user; raises the same 401/403s the other settings POSTs use."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    return user


@app.post("/settings/google-oauth/start", response_class=HTMLResponse)
def start_google_oauth(request: Request, db: Session = Depends(get_db)):
    """Runs the "Sign in with Google" flow using the CURRENTLY SAVED OAuth
    client JSON (same "save first" contract as test-sheets) — opens the
    admin's system browser on this PC, waits for the consent redirect, and
    stores the resulting refresh token encrypted. On success also switches
    google_auth_mode to "oauth" so subsequent Sheets calls use it."""
    _require_settings_admin(request, db)

    client_json = (get_google_oauth_client_json(db) or "").strip()
    if not client_json:
        result = {
            "state": "error",
            "message": "Спочатку вставте й збережіть OAuth Client JSON",
        }
    else:
        try:
            config = parse_client_config(client_json)
            refresh_token = run_authorization_flow(config)
        except OAuthFlowError as exc:
            logger.warning("Google OAuth sign-in failed: %s", exc)
            result = {"state": "error", "message": str(exc)}
        except Exception:
            logger.exception("Google OAuth sign-in failed unexpectedly")
            result = {
                "state": "error",
                "message": "Не вдалося завершити вхід через Google. Спробуйте ще раз",
            }
        else:
            set_setting(db, "google_oauth_refresh_token", refresh_token)
            set_setting(db, "google_auth_mode", "oauth")
            db.commit()
            reset_sheets_cache()
            result = {"state": "success", "message": "Вхід через Google виконано"}

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )


@app.post("/settings/google-oauth/disconnect", response_class=HTMLResponse)
def disconnect_google_oauth(request: Request, db: Session = Depends(get_db)):
    """Clears the stored refresh token and switches back to the service-account
    mode — lets an admin re-run the sign-in flow (e.g. with a different Google
    account) without leaving a stale token behind."""
    _require_settings_admin(request, db)
    set_setting(db, "google_oauth_refresh_token", "")
    set_setting(db, "google_auth_mode", "service_account")
    db.commit()
    reset_sheets_cache()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/settings/materials", response_class=HTMLResponse)
def get_materials_settings(request: Request, db: Session = Depends(get_db)):
    """Screen: material library management (admin). Lists categories with their
    alias rules, plus the count of orders whose colour is still unclassified."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    ensure_seeded(db)
    flash = request.session.pop("materials_flash", None)
    return templates.TemplateResponse(
        request,
        "settings_materials.html",
        {
            "page_title": "Бібліотека матеріалів",
            "user": user,
            "materials": list_materials(db),
            "unresolved_count": unresolved_order_count(db),
            "flash": flash,
        },
    )


@app.post("/settings/materials/alias/add")
def add_material_alias(
    request: Request,
    material_id: int = Form(...),
    pattern: str = Form(...),
    match_type: str = Form("contains"),
    db: Session = Depends(get_db),
):
    _require_settings_admin(request, db)
    try:
        add_alias(db, material_id, pattern, match_type)
        # Apply the new rule to every order, including already-classified ones.
        changed = backfill_orders(db, only_unresolved=False)
        db.commit()
        request.session["materials_flash"] = {
            "kind": "success",
            "message": f"Правило додано. Перекласифіковано робіт: {changed}.",
        }
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@app.post("/settings/materials/alias/{alias_id}/delete")
def remove_material_alias(alias_id: int, request: Request, db: Session = Depends(get_db)):
    _require_settings_admin(request, db)
    delete_alias(db, alias_id)
    # Re-resolve from scratch so orders that only matched the deleted rule are
    # re-evaluated against the remaining rules (may become unresolved again).
    for order in db.scalars(select(Order)).all():
        order.material_id = None
    backfill_orders(db, only_unresolved=False)
    db.commit()
    request.session["materials_flash"] = {"kind": "success", "message": "Правило видалено."}
    return RedirectResponse("/settings/materials", status_code=303)


@app.post("/settings/materials/add")
def create_material(
    request: Request,
    name: str = Form(...),
    is_production: str = Form("on"),
    db: Session = Depends(get_db),
):
    _require_settings_admin(request, db)
    try:
        add_material(db, name, is_production=is_production == "on")
        db.commit()
        request.session["materials_flash"] = {"kind": "success", "message": "Матеріал додано."}
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@app.post("/settings/materials/reclassify")
def reclassify_materials(request: Request, db: Session = Depends(get_db)):
    _require_settings_admin(request, db)
    for order in db.scalars(select(Order)).all():
        order.material_id = None
    changed = backfill_orders(db, only_unresolved=False)
    db.commit()
    request.session["materials_flash"] = {
        "kind": "success",
        "message": f"Перекласифіковано робіт: {changed}.",
    }
    return RedirectResponse("/settings/materials", status_code=303)


@app.get("/settings/recognition", response_class=HTMLResponse)
def get_recognition_settings(request: Request, db: Session = Depends(get_db)):
    """Screen: mail recognition settings (admin). Holds the default-material
    fallback the triage applies to a signal-less milling letter, and points to
    the two editable dictionaries that live elsewhere — the material library
    (synonyms like «врім'янка» → ПММА) and the mail filters (exception
    categories like 3D-друк / моделювання that route a letter out of the queue)."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    flash = request.session.pop("recognition_flash", None)
    return templates.TemplateResponse(
        request,
        "settings_recognition.html",
        {
            "page_title": "Розпізнавання пошти",
            "user": user,
            "materials": list_materials(db),
            "default_material": get_mail_default_material(db),
            "flash": flash,
        },
    )


@app.post("/settings/mail-spool/prune")
def prune_mail_spool(request: Request, db: Session = Depends(get_db)):
    """Delete the mail-spool folders analyze_spool considers safe (empty ones,
    orphans with no letter row, and rejected letters past the retention
    window). Operator-triggered only — never a background job, see
    app/mail_spool.py."""
    _require_settings_admin(request, db)
    removed, freed = prune_spool(db, Path(MAIL_ATTACHMENTS_PATH))
    mb = round(freed / (1024 * 1024), 1)
    request.session["settings_flash"] = {
        "kind": "success",
        "message": (
            f"Прибрано папок: {removed}, звільнено {mb} МБ."
            if removed
            else "Нічого прибирати — спул чистий."
        ),
    }
    return RedirectResponse("/settings#mail-download", status_code=303)


# One self-check probe may not wedge the run. Mirrors the reasoning behind
# mail_sync_service.MAIL_SYNC_DEADLINE_SECONDS: a half-open TLS socket can hang
# an IMAP/Sheets call indefinitely, and here that would stall a threadpool
# worker with the UI showing a spinner forever. Past the deadline the probe is
# abandoned (its thread is left to die on its own) and reported as a failure.
SELFCHECK_STEP_DEADLINE_SECONDS = 20


@app.get("/api/notify-state")
def api_notify_state(request: Request, db: Session = Depends(get_db)):
    """Cheap snapshot the client polls to detect system events worth a popup.

    Deliberately NOT a push channel: the browser compares this against its own
    previous snapshot and raises a toast on a TRANSITION (ok → error, count
    grew). That keeps the trigger logic in one place client-side and means a
    missed poll can never replay an old alert — the next poll just reflects
    reality. Everything here is already computed for the queue page, so this
    costs two scalar counts and two in-memory heartbeat reads.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    status = _queue_sync_status(db, datetime.now())
    release = get_known_update()
    return {
        "sheet": status["sheet"]["state"],
        "mail": status["mail"]["state"],
        "sheet_label": status["sheet"]["label"],
        "mail_label": status["mail"]["label"],
        "orders": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status != "видано", Order.archived_at.is_(None))
        ) or 0,
        "mail_pending": db.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(EmailMessage.status == "нове", EmailMessage.filter_category.is_(None))
        ) or 0,
        # Works a technician corrected in the sheet and nobody has acknowledged
        # yet. A rise means a fresh correction — the client toasts on that, so
        # the operator learns about it even while looking at the machines.
        "changed": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.sheet_changed_at.is_not(None), Order.archived_at.is_(None))
        ) or 0,
        "update": release.version if release else None,
    }


@app.post("/settings/notifications")
async def save_notification_prefs(request: Request, db: Session = Depends(get_db)):
    """Save popup look, placement and which system triggers may fire one.

    Operator-facing (not admin-only): these are per-installation display
    preferences on a single-workstation app, not a security boundary — the same
    reasoning that makes the folder paths operator-editable.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    form = await request.form()
    set_notify_prefs(
        db,
        style=(form.get("notify_style") or "").strip(),
        position=(form.get("notify_position") or "").strip(),
        events=form.getlist("notify_events"),
    )
    db.commit()
    if request.headers.get("HX-Request") == "true":
        return _toast_response("Налаштування сповіщень збережено")
    return RedirectResponse("/settings#notifications", status_code=303)


@app.post("/settings/sheet-weight", response_class=HTMLResponse)
def settings_sheet_weight(request: Request, db: Session = Depends(get_db)):
    """Weigh the spreadsheet's conditional formatting — read-only diagnostic.

    Answers "чому додавання таке повільне" with numbers instead of guesses: a
    document whose day-tabs are copies of yesterday's accumulates per-cell
    conditional-format rules, and every values call then pays for the whole
    document's metadata. Nothing is modified here; cleaning is a separate,
    explicitly requested action.
    """
    _require_settings_admin(request, db)

    if not _sheets_configured(db):
        result = {"state": "error", "message": "Спочатку підключіть Google Таблицю"}
        return templates.TemplateResponse(
            request, "_settings_check_result.html", {"result": result}
        )

    try:
        weight = measure_sheet_weight(open_spreadsheet(db=db))
    except Exception as exc:  # noqa: BLE001 — surface a safe reason, never raw Google text
        logger.warning("Sheet weight probe failed", exc_info=True)
        return templates.TemplateResponse(
            request,
            "_settings_check_result.html",
            {"result": {"state": "error", "message": _sheets_access_error_message(db, exc)}},
        )

    # Thresholds from the measured test-sheet case: a healthy tab carries a
    # handful of rules; thousands per tab is the copy-a-tab disease.
    avg = weight["avg_rules"]
    if avg >= 200:
        state, verdict = "error", "Таблиця сильно роздута умовним форматуванням — це і є причина повільності"
    elif avg >= 50:
        state, verdict = "warning", "Умовного форматування помітно більше норми"
    else:
        state, verdict = "success", "Умовне форматування в нормі — повільність не через нього"

    message = (
        f"{verdict}. Правил: {weight['total_rules']} у {weight['tab_count']} вкладках "
        f"(в середньому {avg} на вкладку, норма 5–20). "
        f"Дрібних діапазонів 1×1/2×1: {weight['tiny_ranges']}. "
        f"Метадані: {weight['payload_mb']} МБ, читались {weight['fetch_seconds']} с."
    )
    logger.info("SHEET-WEIGHT %s", message)
    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": {"state": state, "message": message}}
    )


@app.post("/settings/selfcheck")
def settings_selfcheck(request: Request, db: Session = Depends(get_db)):
    """"Стан системи" self-check — streams NDJSON, one line per probe, as each
    one finishes: {key, name, ok, warn, detail, ms}, then a final
    {done, passed, total, version}.

    Streaming rather than one batched JSON so the UI's progression is real: the
    row lights up when its probe actually starts and settles when it actually
    returns. Reuses the exact probes behind the individual «Перевірити» buttons,
    so green here means green there. Nothing is mutated and no secret ever
    leaves — only задано / не задано and the same classified messages those
    buttons show. Admin + loopback only, like the other settings mutations.

    Every config value is read from the DB up front: the generator body runs
    after the request's session would otherwise be torn down, so it must not
    touch `db`.
    """
    import json as _json
    import shutil
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTimeout

    _require_settings_admin(request, db)

    sheets_ready = _sheets_configured(db)
    imap_ready = _imap_configured(db)
    imap_login = get_imap_login(db)
    imap_password = get_imap_password(db)
    export_path = get_export_folder_path(db)
    technician_path = get_technician_files_path(db)

    def _sheets():
        if not sheets_ready:
            return False, False, "не налаштовано — ID або JSON-ключ порожні"
        # open_spreadsheet() without a session falls back to the *env* sheet id,
        # not the one saved through this screen — so it needs a real session.
        # The request's own is gone by the time the generator body runs, hence a
        # short-lived session this probe owns and closes (same shape as
        # _run_mail_sync_owned_session, minus the watchdog-zombie case).
        probe_db = SessionLocal()
        try:
            spreadsheet = open_spreadsheet(db=probe_db)
            n = len(spreadsheet.worksheets())
        finally:
            probe_db.close()
        return True, False, f"доступ підтверджено · {n} вкладок"

    def _imap():
        if not imap_ready:
            return False, False, "не налаштовано — логін або пароль порожні"
        res = _probe_imap_login(imap_login, imap_password)
        return res["state"] == "success", False, res["message"]

    def _folder(path_str):
        p = (path_str or "").strip()
        if not p:
            return False, False, "шлях не задано"
        pp = Path(p)
        if not pp.exists():
            return False, False, "папку не знайдено за вказаним шляхом"
        if not pp.is_dir():
            return False, False, "шлях вказує не на папку"
        return True, False, "існує й доступна"

    def _disk():
        usage = shutil.disk_usage(Path(DB_PATH).parent)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 2:
            return False, False, f"вільно лише {free_gb:.1f} ГБ (потрібно ≥2 ГБ)"
        return True, free_gb < 10, f"вільно {free_gb:.1f} ГБ"

    def _backup():
        snaps = list_snapshots(DB_PATH)
        if not snaps:
            return True, True, "жодної автоматичної копії ще немає"
        newest = max(s.stat().st_mtime for s in snaps)
        age_days = (_time.time() - newest) / 86400
        return True, age_days > 40, f"остання копія {age_days:.0f} дн. тому"

    def _update():
        rel = get_known_update()
        if rel:
            return True, True, f"доступне оновлення v{rel.version}"
        return True, False, "встановлена версія найновіша"

    steps = [
        ("sheets", "Доступ до Google Таблиці", _sheets),
        ("imap", "IMAP-зʼєднання зі скринькою", _imap),
        ("export", "Папка export доступна на запис", lambda: _folder(export_path)),
        ("technician", "Папка робіт техніків", lambda: _folder(technician_path)),
        ("disk", "Місце на диску (потрібно ≥2 ГБ)", _disk),
        ("backup", "Резервна копія свіжа", _backup),
        ("update", "Наявність оновлення", _update),
    ]

    def _stream():
        passed = 0
        # Manifest first: the UI renders every row (dimmed, named) up front, so
        # the operator sees what is about to be checked instead of rows
        # appearing anonymously one at a time.
        yield _json.dumps(
            {"steps": [{"key": k, "name": n} for k, n, _ in steps]}, ensure_ascii=False
        ) + "\n"
        # daemon threads: an abandoned probe must never hold up shutdown
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selfcheck")
        try:
            for key, name, fn in steps:
                t0 = _time.perf_counter()
                try:
                    ok, warn, detail = pool.submit(fn).result(
                        timeout=SELFCHECK_STEP_DEADLINE_SECONDS
                    )
                except _FTimeout:
                    logger.warning("selfcheck step %s exceeded deadline", key)
                    ok, warn, detail = False, False, (
                        f"немає відповіді понад {SELFCHECK_STEP_DEADLINE_SECONDS} с — перевірку скасовано"
                    )
                    # The wedged worker owns this pool's only thread; give the
                    # remaining steps a fresh one instead of queueing behind it.
                    pool.shutdown(wait=False)
                    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selfcheck")
                except Exception:
                    logger.warning("selfcheck step %s failed", key, exc_info=True)
                    ok, warn, detail = False, False, "перевірка не виконалась"
                ms = int((_time.perf_counter() - t0) * 1000)
                if ok:
                    passed += 1
                yield _json.dumps(
                    {"key": key, "name": name, "ok": ok, "warn": warn, "detail": detail, "ms": ms},
                    ensure_ascii=False,
                ) + "\n"
            yield _json.dumps(
                {"done": True, "passed": passed, "total": len(steps), "version": VERSION},
                ensure_ascii=False,
            ) + "\n"
        finally:
            pool.shutdown(wait=False)

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        # Chunks must reach the browser as they are produced, not buffered into
        # one response — otherwise the streaming is pointless.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/settings/mail-download/toggle")
def toggle_mail_download_all(
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Flip the "auto-download every incoming letter's attachments" toggle
    (admin). Off (default) keeps the selective whitelist behaviour; on pulls
    files from every sender into the spool as mail arrives. `return_to=mail`
    lands back on the triage screen (the toggle is mirrored in its header),
    otherwise on the settings section."""
    _require_settings_admin(request, db)
    new_value = not get_mail_download_all(db)
    set_mail_download_all(db, new_value)
    db.commit()
    message = (
        "Авто-скачування всіх вкладень увімкнено."
        if new_value
        else "Авто-скачування вимкнено — качаються лише довірені відправники."
    )
    if return_to == "mail":
        request.session["toast_flash"] = {"kind": "success", "message": message}
        return RedirectResponse("/mail", status_code=303)
    request.session["settings_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings#mail-download", status_code=303)


@app.post("/settings/recognition/default-material")
def set_recognition_default_material(
    request: Request,
    material_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Set (or clear, with an empty value) the material the triage assumes for a
    milling letter with no material signal. Validated against real catalog names
    so a typo can't silently disable the rule."""
    _require_settings_admin(request, db)
    clean = (material_name or "").strip()
    valid_names = {m.name for m in list_materials(db)}
    if clean and clean not in valid_names:
        request.session["recognition_flash"] = {"kind": "error", "message": "Невідомий матеріал."}
        return RedirectResponse("/settings/recognition", status_code=303)
    set_mail_default_material(db, clean)
    db.commit()
    if clean:
        message = f"Дефолт без сигналу: {clean}."
    else:
        message = "Дефолтний матеріал вимкнено."
    request.session["recognition_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings/recognition", status_code=303)


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


@app.post("/settings/update/check", response_class=HTMLResponse)
def check_update(request: Request, db: Session = Depends(get_db)):
    """Admin-triggered manual "is there a newer build?" probe for the settings
    "Про застосунок" section. Runs the same one-shot check the daily background
    worker does (_update_check_tick, which refreshes the module-level "last
    known release" that the rail banner also reads), then renders the result as
    an HTMX fragment: an install button when a newer version is found, or a
    reassuring "you're on the latest" otherwise. Network failures never surface
    raw errors — fetch_latest_release swallows them and returns None, same as
    the background path.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    _update_check_tick()
    return templates.TemplateResponse(
        request,
        "_update_check_result.html",
        {"release": get_known_update(), "current_version": VERSION},
    )


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
    initial = _normalize_initial(form.get("sheet_initial", ""))

    if not username or not password:
        return RedirectResponse("/settings?error=логін+і+пароль+обов'язкові", status_code=303)

    existing = db.scalar(select(User).where(User.username == username))
    if existing is not None:
        return RedirectResponse("/settings?error=такий+логін+вже+існує", status_code=303)

    if initial is not None:
        err = _validate_initial(db, initial, exclude_user_id=None)
        if err:
            return RedirectResponse(f"/settings?error={quote(err)}", status_code=303)

    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name,
            role=role,
            sheet_initial=initial,
        )
    )
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/users/{user_id}/initial", response_class=HTMLResponse)
async def set_operator_initial(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Assign/change/clear an operator's sheet letter (admin only). Empty clears
    it (that operator's Sum3D writes then leave "Прорахував" untouched)."""
    admin = get_current_user(request, db)
    if admin is None:
        return RedirectResponse("/login", status_code=303)
    if admin.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")

    form = await request.form()
    initial = _normalize_initial(form.get("sheet_initial", ""))
    if initial is not None:
        err = _validate_initial(db, initial, exclude_user_id=user_id)
        if err:
            return RedirectResponse(f"/settings?error={quote(err)}", status_code=303)

    target.sheet_initial = initial
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


# Тріаж пошти й фільтри живуть в app/routers/mail.py.
app.include_router(mail_router)
