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
from app.client_matcher import match_client_name
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
from app.models import ActionLog, AppSetting, Attachment, Client, ClientNameAlias, ClientSenderMemory, Comment, EmailMessage, MailFilterCategory, MailFilterRule, Order, ReworkRecord, StatusEvent, SyncLog, User
from app.sender_memory import list_sender_memories, lookup_sender, remember_sender
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
from app.routers.archive import router as archive_router
from app.routers.stats import router as stats_router
from app.routers.stl import router as stl_router
from app.routers.deps import (
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
    clear_row_fills,
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
from app.stl_preview import build_preview_token, list_stl_files, resolve_preview_folder, resolve_stl_file
from app.update_check import (
    _update_check_tick,
    _update_check_worker,
    download_and_verify,
    get_known_update,
    launch_silent_install,
)

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
# Fast lane: between full syncs, re-read ONLY the current day's tab this often.
# With the worker thread's spreadsheet/worksheet cache warm that's a single
# ~3s API call, so today's technician edits reach the CRM within ~15-20s while
# the expensive 3-tab full sync stays at the interval above. See
# app/sheet_sync_service.py::sync_hot_tab.
SHEET_SYNC_HOT_INTERVAL_SECONDS = 15

# Days operators are actually looking at right now (queue partial=rows polls
# record them). The hot lane unions these with today/yesterday so "the open
# tab in the CRM" is always among the fast-synced ones, whatever day it is.
_viewed_days: dict[date, float] = {}
_VIEWED_DAY_TTL_SECONDS = 120.0
_VIEWED_DAYS_CAP = 2
# `_viewed_days` має ДВОХ письменників у різних потоках: request-хендлери
# (get_queue) вставляють переглянутий день, а фоновий воркер таблиці читає й
# чистить його в _hot_extra_days. Без локу воркер міг упасти на
# «dictionary changed size during iteration», коли оператор відкриває день
# саме під час тіку. Лок дешевий (дві короткі критичні секції), а гонка
# рідкісна й невідтворювана — рівно той баг, який інакше ловиться раз на
# місяць. (Пор. _sync_heartbeats — там лок НЕ потрібен: один письменник на
# ключ і атомарна заміна незмінного значення.)
_viewed_days_lock = Lock()


def _record_viewed_day(day: date | None) -> None:
    if day is None:
        return
    with _viewed_days_lock:
        _viewed_days[day] = monotonic()


def _hot_extra_days() -> set[date]:
    """Recently-viewed days still worth fast-syncing, freshest first, capped so
    a filter-hopping operator can't balloon the 5s tick into a full sync."""
    now = monotonic()
    with _viewed_days_lock:
        # Знімок під локом — далі сортуємо/фільтруємо вже свою копію, не чіпаючи
        # живий словник під час ітерації.
        items = list(_viewed_days.items())
        for day, ts in items:
            if now - ts >= _VIEWED_DAY_TTL_SECONDS:
                _viewed_days.pop(day, None)
    fresh = sorted(
        ((day, ts) for day, ts in items if now - ts < _VIEWED_DAY_TTL_SECONDS),
        key=lambda item: item[1],
        reverse=True,
    )
    return {day for day, _ in fresh[:_VIEWED_DAYS_CAP]}

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


def _sheets_access_error_message(db: Session, exc: BaseException) -> str:
    """Turn a failed spreadsheet open into the one sentence that says what to
    DO about it.

    The two real-world failures look identical in the old catch-all wording
    ("перевірте ID, ключ і доступ"), yet need opposite actions: a 404 means the
    id is wrong, a 403 means the file exists but was never shared with the
    account we authenticate as. Naming the service-account address in the 403
    case matters — it is exactly what has to be pasted into Google's Share
    dialog. Raw Google error text is still never echoed.
    """
    status = None
    if isinstance(exc, gspread.exceptions.APIError):
        status = getattr(getattr(exc, "response", None), "status_code", None)

    if isinstance(exc, gspread.exceptions.SpreadsheetNotFound) or status == 404:
        return "Таблицю з таким ID не знайдено — перевірте ID або вставте посилання на таблицю"

    if status == 403:
        email = get_service_account_email(db)
        if get_google_auth_mode(db) == "oauth":
            return "Акаунт Google не має доступу до цієї таблиці — увійдіть тим акаунтом, що бачить таблицю"
        if email:
            return (
                "Таблиця не відкрита для сервісного акаунта. Відкрийте її в Google → "
                f"«Поділитися» → додайте {email} як Редактора"
            )
        return "Немає доступу до таблиці — надайте сервісному акаунту права Редактора"

    if status in (401, 400):
        return "Google не прийняв облікові дані — перевірте JSON-ключ сервісного акаунта"

    return "Не вдалося відкрити таблицю. Перевірте ID, ключ і доступ до таблиці"


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
    if not (get_google_sheet_id(db) or "").strip():
        return False
    if get_google_auth_mode(db) == "oauth":
        return bool(
            (get_google_oauth_client_json(db) or "").strip()
            and (get_google_oauth_refresh_token(db) or "").strip()
        )
    return bool((get_google_service_account_json(db) or "").strip())


def _imap_configured(db: Session) -> bool:
    return bool((get_imap_login(db) or "").strip() and (get_imap_password(db) or "").strip())


def _run_mail_sync_owned_session(*, trigger: str) -> int:
    """Run one mail sync on a session this function owns and closes — EXCEPT
    after a watchdog timeout, when the hung fetch thread still holds that
    session and closing it from here would yank the connection out from under
    it (sessions aren't thread-safe). In that case the session is deliberately
    leaked to the zombie (daemon thread; a single SQLite connection) and the
    error propagates so the caller can log/heartbeat/toast it."""
    sync_db = SessionLocal()
    timed_out = False
    try:
        # Background goes through sync_mail_background (the module-level name
        # the heartbeat tests monkeypatch); manual through sync_mailbox.
        if trigger == "background":
            return sync_mail_background(sync_db, Path(MAIL_ATTACHMENTS_PATH))
        return sync_mailbox(sync_db, Path(MAIL_ATTACHMENTS_PATH), trigger=trigger)
    except MailSyncTimeoutError:
        timed_out = True
        raise
    finally:
        if not timed_out:
            sync_db.close()


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


def _sync_summary_message(summary: SheetSyncSummary) -> str:
    if summary.tabs_processed == 0:
        return "Підключення працює, але в доступному періоді не знайдено датованих вкладок."
    message = (
        f"Синхронізовано вкладок: {summary.tabs_processed}. "
        f"Нових робіт: {summary.created}, оновлено: {summary.updated}, "
        f"без змін: {summary.unchanged}."
    )
    if summary.deleted:
        message += f" Видалено (немає в таблиці): {summary.deleted}."
    return message


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


# Shown when a table-writing action is attempted while sync is paused. The
# action is refused and NOTHING changes — not even the DB — so there is no
# divergence for the resume read to revert. The operator retries after resume.
_SYNC_PAUSED_MSG = (
    "Синхронізацію таблиці призупинено — зміну не збережено. "
    "Зніміть паузу, щоб продовжити."
)


def _sum_units(orders) -> int:
    """Total units across the given orders. Sums only cleanly-integer quantity
    strings — the sheet's quantity column is free text, so ranges ("13-23") or
    blanks are skipped rather than guessed at, keeping the count honest."""
    total = 0
    for order in orders:
        value = (order.quantity or "").strip()
        if value.isdigit():
            total += int(value)
    return total


# Вхід, ліцензія і кабінет оператора живуть в app/routers/auth.py.
app.include_router(auth_router)


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
    # `partial=rows` returns only the queue-rows fragment (for the 15s HTMX
    # poll that keeps the table in step with the sheet without a full reload).
    partial: str = "",
    # Order id the page should scroll to and highlight once loaded — set by the
    # «Останні дії» popup when the action it points at lives on another day tab
    # or behind different filters, so the jump survives the navigation.
    focus: str = "",
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

    # Fetch all orders (eager-load material for the queue's material badge)
    all_orders = db.scalars(
        select(Order).options(selectinload(Order.material)).order_by(Order.id.desc())
    ).all()

    # Define date boundaries
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    # Working space = active orders within the retention window. Archived orders
    # (removed from Google or explicitly archived) and orders older than
    # RETENTION_DAYS drop out of every working view here — they stay in the DB
    # and are reachable on the Archive screen. Done in Python (not SQL) because
    # the business date is derived from sheet_tab, not a stored column, and the
    # order set is small (tens per day, a few thousand total).
    retention_cutoff = today - timedelta(days=RETENTION_DAYS)
    all_orders = [
        o
        for o in all_orders
        if o.archived_at is None and _order_date(o) >= retention_cutoff
    ]

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
    # Mirror the sheet's own hierarchy in the neutral, unfiltered view: internal
    # lab works (the main table region) above the наряд-less client/mail rows
    # (the region below it) — the queue table renders this flat `orders` list, so
    # the ordering has to happen here. Only when the operator hasn't narrowed or
    # re-sorted anything (source=all, ready=all, no explicit column sort, no
    # overdue shortcut), so a deliberate sort/filter still wins. Stable: the
    # urgency order within each group is preserved, лаб rows just float on top.
    if source == "all" and ready == "all" and not sort and not show_overdue:
        orders.sort(key=lambda o: 0 if o.source == "lab" else 1)

    orders_lab = [o for o in orders if o.source != "email"]
    orders_email = [o for o in orders if o.source == "email"]

    # Pop flashes only on a full-page render — the 15s poll (partial="rows")
    # would otherwise consume them before the real navigation shows them.
    sync_flash = request.session.pop("sync_flash", None) if partial != "rows" else None
    toast_flash = request.session.pop("toast_flash", None) if partial != "rows" else None
    # Newest-first, matching the /mail triage list exactly — the pinned widget
    # is a peek of the SAME queue, so the two must agree on order (an opposite
    # sort made the widget's top rows look like different letters).
    pending_emails = db.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.status == "нове",
            # Rule-filtered letters (3D print, accounting, spam) live on the
            # triage screen's «Відфільтровані» tab — keep the queue widget to
            # actual milling work.
            EmailMessage.filter_category.is_(None),
        )
        .options(selectinload(EmailMessage.attachments))
        .order_by(
            EmailMessage.received_at.desc().nullslast(),
            EmailMessage.created_at.desc(),
            EmailMessage.id.desc(),
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
    # Day-strip days come from the WORKING set (already filtered to active +
    # within the retention window above), so the strip shows only days the
    # operator still works with — never archived/older days and never a phantom
    # "today" without a real tab. _date_window uses `today` only to pick the
    # default page (lands on the newest real day when today isn't among them).
    date_universe = sorted({_order_date(o) for o in all_orders})
    date_tabs, current_date_page, total_date_pages = _date_window(date_universe, today, date_page)

    # Query string of the currently active filters, so the 15s poll fragment
    # re-requests the exact same view it lives in. Built from the validated
    # params (not request.query_params) so it also works when get_queue is
    # called directly in tests, and reflects clamped/validated values.
    _qs_items: list[tuple[str, str]] = []
    if show_overdue:
        _qs_items.append(("overdue", "1"))
    _qs_items += [("period", period), ("ready", ready), ("source", source)]
    if date_param:
        _qs_items.append(("date", date_param))
        _qs_items.append(("date_page", str(current_date_page)))
    if sort:
        _qs_items += [("sort", sort), ("dir", sort_dir)]
    rows_qs = urlencode(_qs_items)

    # The single day this operator is actually looking at. Two consumers:
    # the hot sync lane (so the open tab is always among the fast-synced ones)
    # and the manual-add form, which writes its row into THIS tab.
    # It must not be `selected_date` alone: picking a day from the date strip
    # sets it, but the «Завтра»/«Вчора» period tabs do not put a date in the
    # URL at all — an add made from those then silently fell back to today.
    if selected_date is not None:
        viewed_day = selected_date
    elif period == "yesterday":
        viewed_day = today - timedelta(days=1)
    elif period == "tomorrow":
        viewed_day = today + timedelta(days=1)
    elif period == "today" and not show_overdue:
        viewed_day = today
    else:
        viewed_day = None  # "earlier"/overdue span many days — no single tab

    context = {
            "page_title": "Черга робіт",
            "orders": orders,
            "orders_lab": orders_lab,
            "orders_email": orders_email,
            # Sum of units across the currently-filtered view (period/source/
            # ready/date/overdue all already applied to `orders`). Only cleanly
            # numeric quantities count; ranges/blanks are skipped rather than
            # guessed. Shown next to the "N у вигляді" live counter.
            "total_units": _sum_units(orders),
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
            "toast_flash": toast_flash,
            "pending_emails": pending_emails,
            "pending_mail_count": pending_mail_count,
            "selected_date": selected_date,
            "date_tabs": date_tabs,
            "date_page": current_date_page,
            "total_date_pages": total_date_pages,
            "sort": sort,
            "sort_dir": sort_dir,
            "rows_qs": rows_qs,
            "sync_speed": SYNC_SPEED_PRESETS,
            "sync_speed_active": sync_control.get_speed_preset(),
            "sync_screen_seconds": get_sync_speed()["screen"],
            "viewed_tab": viewed_day.strftime("%d.%m.%y") if viewed_day else "",
            "sync_paused": sync_control.is_paused(),
            "focus_order_id": focus.strip() if focus.strip().isdigit() else "",
    }

    _record_viewed_day(viewed_day)

    # The screen poll asks for just the rows block; everything else (sidebar
    # counts, KPIs) refreshes on a full navigation or a manual sync.
    if partial == "rows":
        return templates.TemplateResponse(request, "_queue_rows.html", context)

    return templates.TemplateResponse(request, "queue.html", context)


@app.post("/sync-speed", response_class=HTMLResponse)
def set_sync_speed(request: Request, preset: str = Form(""), db: Session = Depends(get_db)):
    """Switch the global sync-speed preset (queue side panel's segmented
    control). Global on purpose: the hot lane is one worker for the whole
    process, so the fastest interest wins for everyone. Unknown preset values
    degrade to no-op (same spirit as the queue's filter params)."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    sync_control.set_speed_preset(preset)
    return templates.TemplateResponse(
        request,
        "_sync_speed_seg.html",
        {
            "sync_speed": SYNC_SPEED_PRESETS,
            "sync_speed_active": sync_control.get_speed_preset(),
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
        # Той самий N+1, що й на видачі: рядок пошуку рендерить маркування.
        all_orders = db.scalars(
            select(Order).options(selectinload(Order.material))
        ).all()
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


def _back_to_queue(request: Request) -> str:
    """Path+query of the queue page the request came from, for post-action
    redirects that must keep the operator's active filters. Falls back to "/"
    when there's no usable Referer. Scheme and host are discarded, so only a
    local path is ever returned (no open-redirect surface); a Referer pointing
    outside the queue root ("/") is ignored too."""
    referer = request.headers.get("referer")
    if not referer:
        return "/"
    parts = urlsplit(referer)
    if parts.path not in ("", "/"):
        return "/"
    return "/" + (f"?{parts.query}" if parts.query else "")


def _synced_day_tabs(request: Request) -> set[str]:
    """The explicit sidebar day (?date=dd.mm.yy) the sync was launched from,
    as a set of sheet-tab titles to force-include. Empty when there's no valid
    date filter — the default three-day window then applies unchanged."""
    referer = request.headers.get("referer")
    if not referer:
        return set()
    date_values = parse_qs(urlsplit(referer).query).get("date", [])
    return {value for value in date_values if _parse_sheet_tab(value) is not None}


@app.post("/sync/pause")
def toggle_sync_pause(request: Request, db: Session = Depends(get_db)):
    """Pause or resume ALL Google Sheet traffic (read AND write) from the web.

    The same switch the tray menu flips (app/sync_control.py) — one process, one
    flag. Admin-only: an accidental pause silently stops the queue from tracking
    the sheet, so it isn't an operator-level toggle. Returns to the queue with a
    toast; a banner there keeps an active pause visible so it's never forgotten."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    sync_control.set_paused(not sync_control.is_paused())
    paused = sync_control.is_paused()
    request.session["sync_flash"] = {
        "kind": "info",
        "message": (
            "Синхронізацію таблиці призупинено — система не читає й не пише таблицю."
            if paused
            else "Синхронізацію відновлено — читаю свіжу таблицю."
        ),
    }
    return RedirectResponse("/", status_code=303)


@app.post("/sheets/sync")
def sync_sheets(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    if sync_control.is_paused():
        request.session["sync_flash"] = {
            "kind": "info",
            "message": "Синхронізацію призупинено. Зніміть паузу, щоб синхронізувати.",
        }
        return RedirectResponse("/", status_code=303)

    # If the operator triggered the sync while viewing a specific day (the
    # sidebar "Дні" strip sets ?date=dd.mm.yy), force-include that tab so a
    # manual sync of an older day reconciles deletions there too — the periodic
    # window (yesterday/today/tomorrow) never revisits old tabs on its own.
    include_tabs = _synced_day_tabs(request)

    try:
        summary = sync_google_sheets(db, include_tabs=include_tabs)
    except SheetSyncError as exc:
        request.session["sync_flash"] = {"kind": "error", "message": str(exc)}
    else:
        request.session["sync_flash"] = {
            "kind": "success",
            "message": _sync_summary_message(summary),
        }
    # Return to the exact queue view the operator synced from (period/ready/
    # source/date/sort filters live in the query string) instead of resetting
    # to bare "/". Only the path+query of a same-app Referer is used — scheme/
    # host are dropped, so this can't become an open redirect.
    return RedirectResponse(_back_to_queue(request), status_code=303)


@app.post("/sheets/import-history")
def import_sheet_history(request: Request, db: Session = Depends(get_db)):
    """One-off «import the WHOLE sheet»: pull EVERY dated tab, not just the
    periodic today±1 window, so the queue's day-strip gains every historical
    day the sheet holds (arrows then page through them). Deliberately manual —
    it's a heavier run (one proxy read per tab) that the operator asks for once;
    the background sync stays fast. Admin + loopback, same gate as the queue's
    plain sync button."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    try:
        summary = sync_google_sheets(db, trigger="manual", full_history=True)
    except SheetSyncError as exc:
        request.session["sync_flash"] = {"kind": "error", "message": str(exc)}
    else:
        request.session["sync_flash"] = {
            "kind": "success",
            "message": "Історію таблиці імпортовано. " + _sync_summary_message(summary),
        }
    return RedirectResponse("/", status_code=303)


@app.post("/orders/{order_id}/sum3d-id", response_class=HTMLResponse)
async def set_sum3d_id(
    request: Request,
    order_id: int,
    # Default "" (not Form(...)): an EMPTY value is a valid, meaningful input —
    # the operator deleting a mistakenly-entered Sum3D to send the work back to
    # «можна брати». Form(...) treated an empty form field as missing and 422'd,
    # so clearing was impossible from the UI at all.
    sum3d_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            {"order": order, "statuses": STATUSES, "sync_error": _SYNC_PAUSED_MSG},
        )

    value = sum3d_id.strip() or None
    # Entering a Sum3D ID IS the "I calculated this in Sum3D" moment, so the
    # portal stamps the operator's letter into the "Прорахував" column — М for a
    # normal work, Х for a rework — matching the lab's existing by-hand
    # convention. Only when the operator actually has a letter assigned and a
    # value is being set (never on a clear); an operator without a letter just
    # gets the Sum3D written, "Прорахував" left as-is.
    initial = (user.sheet_initial or "").strip() or None
    stamp = initial if (initial and value) else None
    rework = order.active_rework
    # Full before-snapshot so "Скасувати" reverts EVERYTHING this action touched
    # (Sum3D + the auto-stamped letter + the auto-advanced status), not just the
    # Sum3D cell — a real "крок назад".
    if rework is not None:
        before = {"rework.sum3d_id": rework.sum3d_id, "rework.calculated_raw": rework.calculated_raw}
    else:
        before = {"sum3d_id": order.sum3d_id, "calculated_raw": order.calculated_raw, "status": order.status}

    if rework is not None:
        # A reworked job — the ID the operator types is the redo calculation's
        # Sum3D (column W), NOT the original job's ID (column L, left intact as
        # the "previous calculation" the operator reviews to avoid repeating the
        # mistake — see the order passport's rework block). The letter goes to
        # the rework "Прорахував" (column Х), the redo counterpart of М.
        rework.sum3d_id = value
        if stamp:
            rework.calculated_raw = stamp
        sync_error = _write_rework_sum3d(db, order, value or "", letter=stamp)
        after = {"rework.sum3d_id": rework.sum3d_id, "rework.calculated_raw": rework.calculated_raw}
        note = f"Sum3D переробки → {value}" if value else "Sum3D переробки очищено"
        undo_field = "rework.sum3d_id"
    else:
        order.sum3d_id = value
        write_fields = {"sum3d_id"}
        if stamp:
            order.calculated_raw = stamp
            write_fields.add("calculated_raw")
            # The letter in М is the "прораховано" marker, so advance the DB
            # status to match (never downgrade a further state), recording the
            # real logged-in operator who calculated it.
            if order.status in ("нове", "прийнято"):
                order.status = "прораховано"
                db.add(StatusEvent(
                    order_id=order.id, operator_id=user.id,
                    status="прораховано", actor=user.username,
                ))
        sync_error = _write_sheet_fields(db, order, write_fields)
        after = {"sum3d_id": order.sum3d_id, "calculated_raw": order.calculated_raw, "status": order.status}
        note = f"Sum3D → {value}" if value else "Sum3D очищено"
        undo_field = "sum3d_id"

    log_entry = log_action(
        db, order=order, operator=user, action_type="sum3d", field=undo_field,
        old=json.dumps(before, ensure_ascii=False),
        new=json.dumps(after, ensure_ascii=False), note=note,
    )
    db.commit()
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    response = templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES, "sync_error": sync_error}
    )
    if sync_error is None:
        _attach_action_toast(response, log_entry, note)
    return response


@app.post("/orders/{order_id}/operator", response_class=HTMLResponse)
async def set_operator(request: Request, order_id: int, operator: str = Form(""), db: Session = Depends(get_db)):
    """Set OR clear the «Прорахував» cell (column М → Order.calculated_raw): who
    calculated the work. The operator types the letter/code straight in the queue
    row and it writes back to the sheet; an empty value clears it (the ✕ button
    just empties the input). Logged as an undoable "operator" action. Does not
    touch Sum3D or readiness (which key off job_code + sum3d_id, not this cell).

    Default "" (not Form(...)): an empty value is a valid input — clearing the
    cell — exactly like set_sum3d_id, where Form(...) would 422 a blank field."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            {"order": order, "statuses": STATUSES, "sync_error": _SYNC_PAUSED_MSG},
        )

    value = operator.strip()
    old_value = order.calculated_raw
    if (old_value or "") == value:
        # No change — return the row untouched, no log, no toast.
        attach_export_folder_uris(db, [order])
        attach_job_code_folder_uris(db, [order])
        return templates.TemplateResponse(
            request, "_order_row.html", {"order": order, "statuses": STATUSES, "sync_error": None}
        )

    order.calculated_raw = value
    sync_error = _write_calculated(db, order, value)
    note = f"оператор → {value}" if value else "оператора очищено"
    log_entry = log_action(
        db, order=order, operator=user, action_type="operator", field="calculated_raw",
        old=old_value or "", new=value, note=note,
    )
    db.commit()
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    response = templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES, "sync_error": sync_error}
    )
    if sync_error is None:
        _attach_action_toast(response, log_entry, note)
    return response


@app.post("/orders/{order_id}/cam-comment", response_class=HTMLResponse)
async def set_cam_comment(
    request: Request,
    order_id: int,
    cam_comment: str = Form(""),
    db: Session = Depends(get_db),
):
    """Inline edit of the CAM comment straight from the queue row. Unlike the
    passport's /comments (which appends to the two-way history preserving other
    people's sheet edits), this SETS the CAM-comment cell to exactly what the
    operator typed — the "enter/edit my own note in the row" flow. Writes back
    to the sheet's comment column for lab orders, same discipline as sum3d-id."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            {"order": order, "statuses": STATUSES, "sync_error": _SYNC_PAUSED_MSG},
        )

    old_comment = order.cam_comment
    order.cam_comment = cam_comment.strip() or None
    if (old_comment or "") != (order.cam_comment or ""):
        log_action(
            db, order=order, operator=user, action_type="cam_comment", field="cam_comment",
            old=old_comment or "", new=order.cam_comment or "",
            note=(f"коментар → {order.cam_comment}" if order.cam_comment else "коментар очищено"),
        )
    db.commit()  # persist immediately — the row must feel instantly saved
    db.refresh(order)

    # Mirror to the sheet in the background so a slow Google write never stalls
    # the inline edit (see _write_sheet_fields_background).
    _write_sheet_fields_background(order.id, {"cam_comment"})

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    return templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES, "sync_error": None}
    )


@app.get("/orders/new", response_class=HTMLResponse)
def new_order_form(
    request: Request, db: Session = Depends(get_db), error: str = "",
    work_type: str = "client", material_color: str = "", client_name: str = "",
    work_order_no: str = "", kind: str = "", quantity: str = "", sum3d_id: str = "",
    job_code: str = "", technician_name: str = "",
    return_to: str = "", target_tab: str = "",
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "new_order.html",
        {
            "user": user,
            "today": date.today().strftime("%d.%m.%y"),
            "error": error or None,
            # Carried through a failed validation so the retry still writes to
            # the day tab the operator started from and lands back there. This
            # page is reached almost only via _back, so losing them here meant
            # the second attempt silently fell back to today's tab.
            "return_to": return_to,
            "target_tab": target_tab,
            "form": {
                "work_type": work_type if work_type in ("client", "lab") else "client",
                "client_name": client_name, "work_order_no": work_order_no,
                "kind": kind, "material_color": material_color,
                "quantity": quantity, "sum3d_id": sum3d_id,
                "job_code": job_code, "technician_name": technician_name,
            },
        },
    )


_MAX_MANUAL_ROWS = 30

# Server-side double-submit guard for manual adds. The submit button disables
# itself client-side, but an F5 re-POST, back-button resubmit, or a browser
# retry still reaches the server and would append the same rows to the sheet
# AGAIN. Keyed by user id; an identical payload within the window is treated
# as the same submit and answered with the normal redirect, writing nothing.
# In-process state is enough: the app runs as a single local process.
_MANUAL_ADD_DEDUP_SECONDS = 30.0
_recent_manual_adds: dict[int, tuple[str, float]] = {}


@app.post("/orders/new")
def create_manual_order(
    request: Request,
    work_type: str = Form("client"),
    return_to: str = Form(""),
    target_tab: str = Form(""),
    client_name: list[str] = Form([]),
    work_order_no: list[str] = Form([]),
    kind: list[str] = Form([]),
    material_color: list[str] = Form([]),
    quantity: list[str] = Form([]),
    sum3d_id: list[str] = Form([]),
    job_code: list[str] = Form([]),
    technician_name: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    """Add one OR several works by hand and mirror them into today's sheet tab.

    Each field arrives as a parallel list (one entry per form row), so a single
    submit can add several clients or several lab наряди at once. Two kinds:

      * client (default) — наряд-less client rows (client name in "Вид роботи",
        painted the lab's pending blue), source="sheet_client".
      * lab — normal internal works: наряд in "Номер наряду", вид in "Вид
        роботи", not painted, source="lab".

    The whole batch is written as one contiguous block in a single sheet call.
    Each row is linked by row_number so the next sync updates it in place."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    work_type = work_type if work_type in ("client", "lab") else "client"
    is_lab = work_type == "lab"

    # Send the operator back to the exact queue view they submitted from — same
    # day tab, same filters — instead of resetting them to the default queue.
    # Only a same-origin relative path is accepted: an absolute or
    # protocol-relative value would turn this form into an open redirect.
    default_target = f"/?source={'lab' if is_lab else 'client'}"
    target = return_to.strip() if isinstance(return_to, str) else ""
    if not target.startswith("/") or target.startswith("//"):
        target = default_target

    # The day tab on screen wins over "today" — see _append_manual_rows_warm.
    # Validated as a real dd.mm.yy here so a hand-crafted value can only ever
    # miss and fall back, never reach the sheet layer as junk. Resolved before
    # _back so a failed validation can carry it into the retry.
    wanted_tab = target_tab.strip() if isinstance(target_tab, str) else ""
    if wanted_tab and _parse_sheet_tab(wanted_tab) is None:
        wanted_tab = ""

    def _back(message: str):
        params = urlencode({
            "error": message, "work_type": work_type,
            "return_to": target, "target_tab": wanted_tab,
        })
        return RedirectResponse(f"/orders/new?{params}", status_code=303)

    def _at(values: list[str], i: int) -> str:
        return values[i].strip() if i < len(values) else ""

    if sync_control.is_paused():
        return _back(_SYNC_PAUSED_MSG)

    row_count = max(
        len(client_name), len(work_order_no), len(kind), len(material_color),
        len(quantity), len(sum3d_id), len(job_code), len(technician_name),
    )
    if row_count == 0:
        return _back("Додайте хоча б одну роботу.")
    if row_count > _MAX_MANUAL_ROWS:
        return _back(f"Забагато рядків за раз (макс. {_MAX_MANUAL_ROWS}).")

    # Build the per-row work list, validating each non-empty row. Fully blank
    # rows (an extra field-set the operator left empty) are silently skipped.
    works: list[dict] = []
    for i in range(row_count):
        row_client = _at(client_name, i)
        row_naryad = _at(work_order_no, i)
        row_kind = _at(kind, i)
        row_material = _at(material_color, i)
        row_qty = _at(quantity, i)
        row_sum3d = _at(sum3d_id, i)
        row_job = _at(job_code, i)
        row_tech = _at(technician_name, i)

        if is_lab:
            if not any((row_naryad, row_kind, row_material, row_job, row_tech, row_sum3d)):
                continue  # empty lab row
            works.append({
                "source": "lab", "work_order_no": row_naryad, "kind": row_kind,
                "e_value": row_kind, "material_color": row_material, "quantity": row_qty,
                "job_code": row_job, "technician_name": row_tech, "sum3d_id": row_sum3d,
            })
        else:
            if not any((row_client, row_material, row_qty, row_job, row_tech, row_sum3d)):
                continue  # empty client row
            if not row_client:
                return _back(f"Рядок {i + 1}: вкажіть імʼя клієнта.")
            if not row_material:
                return _back(f"Рядок {i + 1}: вкажіть матеріал / колір.")
            works.append({
                "source": "sheet_client", "client_name": row_client,
                "e_value": row_client, "material_color": row_material, "quantity": row_qty,
                "job_code": row_job, "technician_name": row_tech, "sum3d_id": row_sum3d,
            })

    if not works:
        return _back("Заповніть хоча б одну роботу.")

    # Double-submit guard (see _recent_manual_adds): the exact same batch from
    # the same operator inside the window is a resubmit, not a second intent.
    fingerprint = repr((work_type, works))
    now_ts = monotonic()
    last = _recent_manual_adds.get(user.id)
    if last is not None and last[0] == fingerprint and (now_ts - last[1]) < _MANUAL_ADD_DEDUP_SECONDS:
        return RedirectResponse(target, status_code=303)

    # Append the whole batch on the warm write-back worker (cached spreadsheet/
    # worksheet) in ONE sheet call — the cold request thread would re-pay ~40s of
    # open+worksheet through the lab proxy per call. The worker resolves the
    # newest dated tab ≤ today (today's tab often isn't created yet) and returns
    # which tab it actually wrote to, so the orders land on the same day.
    try:
        result = _sheet_writeback_pool.submit(
            _append_manual_rows_warm, date.today(), works,
            paint_blue=(not is_lab),
            placement=("lab" if is_lab else "client"),
            target_tab=wanted_tab,
        ).result(timeout=120)
    except Exception as exc:  # noqa: BLE001 — surface any sheet failure to the operator
        logger.exception("Manual order sheet write failed")
        return _back(f"Не вдалося записати в таблицю: {exc}")
    if result is None:
        return _back("У таблиці немає жодної датованої вкладки — створіть день у таблиці спершу.")
    tab, note_rows = result

    ensure_seeded(db)
    alias_rows = load_alias_rows(db)
    name_by_id = material_id_by_name(db)
    created_ids: list[int] = []
    for work, note_row in zip(works, note_rows):
        if work["source"] == "lab":
            order = Order(
                source="lab", sheet_tab=tab, row_number=note_row - HEADER_ROWS,
                work_order_no=work["work_order_no"] or None, kind=work["kind"] or None,
                material_color=work["material_color"] or None, quantity=work["quantity"] or None,
                job_code=work["job_code"] or None, technician_name=work["technician_name"] or None,
                sum3d_id=work["sum3d_id"] or None,
                status="прийнято" if work["sum3d_id"] else "нове",
            )
        else:
            order = Order(
                source="sheet_client", sheet_tab=tab, row_number=note_row - HEADER_ROWS,
                client_name=work["client_name"], material_color=work["material_color"] or None,
                quantity=work["quantity"] or None, job_code=work["job_code"] or None,
                technician_name=work["technician_name"] or None,
                sum3d_id=work["sum3d_id"] or None, status="нове",
            )
        order.material_id = resolve_material_id(order.material_color, alias_rows, name_by_id)
        db.add(order)
        db.flush()
        db.add(StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username))
        # Adding a work by hand IS an operator action, so it belongs in the
        # journal and the «Останні дії» popup — otherwise a work the operator
        # just created is the one thing they cannot jump back to. Logged as
        # "create": listed and locatable, but deliberately NOT undoable — «Крок
        # назад» is a quick low-friction click and must never silently delete a
        # row from the shared sheet. Removing a work stays the explicit delete
        # button, which asks first.
        log_action(
            db, order=order, operator=user, action_type="create",
            note=f"додано вручну: {order.work_order_no or order.client_name or ('#' + str(order.id))}",
        )
        created_ids.append(order.id)

    db.add(
        SyncLog(
            direction="db_to_sheet", sheet_tab=tab, status="ok",
            message=f"manual {work_type} ×{len(created_ids)}: рядки {note_rows}",
        )
    )
    db.commit()

    # Record this successful submit so an immediate resubmit (F5/back) is
    # recognised and skipped above. Prune stale entries so the dict can't grow
    # unbounded across a long-running process.
    _recent_manual_adds[user.id] = (fingerprint, now_ts)
    for uid, (_, ts) in list(_recent_manual_adds.items()):
        if now_ts - ts >= _MANUAL_ADD_DEDUP_SECONDS:
            _recent_manual_adds.pop(uid, None)

    return RedirectResponse(target, status_code=303)


@app.post("/orders/{order_id}/change-seen", response_class=HTMLResponse)
async def dismiss_sheet_change(
    request: Request, order_id: int, db: Session = Depends(get_db)
):
    """Clear the "technician corrected this row" mark once the operator has
    looked at it.

    Dismissal is theirs alone — no timer (user decision 25.08.26). The mark
    exists to stop someone milling a version of the work that has since been
    corrected, and a change that expires on its own can expire during a break,
    which is exactly when it would have been missed. Re-renders the row so the
    badge disappears without reloading the queue."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="роботу не знайдено")

    if order.sheet_changed_at is not None:
        db.add(
            StatusEvent(
                order_id=order.id, operator_id=user.id, status=order.status,
                actor=user.username,
                note=f"переглянув зміни техніка: {order.sheet_changed_fields or '—'}",
            )
        )
        order.sheet_changed_at = None
        order.sheet_changed_fields = None
        db.commit()

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])
    return templates.TemplateResponse(
        request, "_order_row.html",
        {"order": order, "statuses": STATUSES, "sync_error": None},
    )


@app.post("/orders/{order_id}/delete")
async def delete_order(
    request: Request,
    order_id: int,
    inline: str = Form(""),
    db: Session = Depends(get_db),
):
    """Remove a work from the queue and blank its row in the sheet.

    Archive, don't destroy: the order keeps its history and moves to «Архів»,
    matching what a row vanishing from the sheet already does (see
    app/sync.py). Deleting the DB row instead would drop its StatusEvents and
    comments, and the next sync would happily re-import the work anyway.

    The sheet row is BLANKED on the background writer, so the operator isn't
    held for a Google round-trip through the lab proxy. Email-sourced works
    (source="email") never had a sheet row — for them this is DB-only.

    Two callers, two replies. From the work card (``inline`` unset) the page
    the operator is looking at no longer exists, so we send them to the queue.
    From a queue row (``inline``) we reply with the plain toast and no redirect:
    the operator keeps the day tab, filters and scroll they were working in, and
    the row is removed client-side (see _order_row.html). Deliberately NOT a
    body swapped over the row — HTMX fires HX-Trigger on the requesting element,
    so swapping the row away first detaches the form and the toast event never
    bubbles to the listener on <body>. The row would vanish silently.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="роботу не знайдено")
    if order.archived_at is not None:
        return _toast_response("Робота вже в архіві", kind="info")

    # Delete blanks the sheet row, so it counts as a table write — refused while
    # paused (deletion also archives the order, which we must not do half-way).
    if sync_control.is_paused():
        return _toast_response(_SYNC_PAUSED_MSG, kind="info")

    order.archived_at = datetime.utcnow()
    db.add(
        StatusEvent(
            order_id=order.id, operator_id=user.id, status=order.status,
            actor=user.username, note="видалено з черги",
        )
    )
    # Logged so the delete is both visible in the journal and reversible: undo
    # un-archives the work and re-fills the sheet row it blanked. The most
    # valuable undo of all — a mis-clicked delete is otherwise retyped by hand.
    log_action(
        db, order=order, operator=user, action_type="delete", field="archived_at",
        old="", new="archived",
        note=f"видалено з черги: {order.work_order_no or order.client_name or ('#' + str(order.id))}",
    )
    db.commit()

    if order.source in ("lab", "sheet_client") and order.sheet_tab and order.row_number:
        _clear_sheet_row_background(order.sheet_tab, order.row_number)
        message = "Роботу видалено з черги, рядок у таблиці очищено"
    else:
        message = "Роботу видалено з черги"

    if request.headers.get("HX-Request") == "true":
        if isinstance(inline, str) and inline.strip():
            return _toast_response(message)
        response = _toast_response(message)
        # Хай сторінка перемалюється — рядок має зникнути з черги одразу.
        response.headers["HX-Redirect"] = "/"
        return response
    return RedirectResponse("/", status_code=303)


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

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            {"order": order, "statuses": STATUSES, "sync_error": _SYNC_PAUSED_MSG},
        )

    old_status = order.status
    order.status = status
    sheet_fields = apply_status_markers(
        order,
        status,
        actor=user.full_name or user.username,
    )
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=status, actor=user.username)
    )
    log_entry = None
    if status != old_status:
        log_entry = log_action(
            db, order=order, operator=user, action_type="status",
            field="status", old=old_status, new=status,
            note=f"статус: {old_status} → {status}",
        )
    sync_error = _write_sheet_fields(db, order, sheet_fields)
    db.commit()
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    response = templates.TemplateResponse(
        request, "_order_row.html", {"order": order, "statuses": STATUSES, "sync_error": sync_error}
    )
    if log_entry is not None and sync_error is None:
        _attach_action_toast(response, log_entry, f"статус → {status}")
    return response


# Action types a "крок назад" can revert, newest-first selection order.
def _undo_response(outcome: UndoOutcome) -> Response:
    """Один переклад результату дії у відповідь HTMX. `refresh_queue` стає
    HX-Trigger `refresh-queue`, щоб опитувана черга показала змінений рядок
    одразу, а не через 15с."""
    triggers = {"refresh-queue": True} if outcome.refresh_queue else None
    return _toast_response(outcome.message, kind=outcome.kind, triggers=triggers)


def _perform_undo(db: Session, user: "User", entry: ActionLog) -> Response:
    return _undo_response(perform_undo(db, user, entry))


@app.post("/actions/{action_id}/undo")
async def undo_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    """Undo one specific logged action by id (guards: own action, once, within
    the window, sync not paused), then revert it via _perform_undo."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    entry = db.get(ActionLog, action_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="дію не знайдено")
    if entry.operator_id != user.id:
        return _toast_response("Скасувати можна лише власну дію", kind="error")
    if entry.undone_at is not None:
        return _toast_response("Цю дію вже скасовано", kind="error")
    if entry.created_at is not None and entry.created_at < datetime.utcnow() - timedelta(seconds=UNDO_WINDOW_SECONDS):
        return _toast_response("Вікно скасування минуло", kind="error")
    if sync_control.is_paused():
        return _toast_response(_SYNC_PAUSED_MSG, kind="info")

    return _perform_undo(db, user, entry)


@app.post("/actions/undo-last")
async def undo_last_action(request: Request, db: Session = Depends(get_db)):
    """«Крок назад» — the static undo button. Finds THIS operator's most recent
    still-undoable action (any of UNDOABLE_ACTION_TYPES — Sum3D, status, operator,
    CAM comment, delete — not yet undone, inside the window) and reverts it.
    Pressing it again steps back through earlier actions. Replaces the per-action
    «Скасувати» toast affordance."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if sync_control.is_paused():
        return _toast_response(_SYNC_PAUSED_MSG, kind="info")

    cutoff = datetime.utcnow() - timedelta(seconds=UNDO_WINDOW_SECONDS)
    entry = (
        db.query(ActionLog)
        .filter(
            ActionLog.operator_id == user.id,
            ActionLog.action_type.in_(UNDOABLE_ACTION_TYPES),
            ActionLog.undone_at.is_(None),
            ActionLog.created_at >= cutoff,
        )
        .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
        .first()
    )
    if entry is None:
        return _toast_response("Немає що скасувати", kind="info")

    return _perform_undo(db, user, entry)


def _perform_redo(db: Session, user: "User", entry: ActionLog) -> Response:
    return _undo_response(perform_redo(db, user, entry))


@app.post("/actions/redo-last")
async def redo_last_action(request: Request, db: Session = Depends(get_db)):
    """«Крок вперед» — the static redo button. Re-applies THIS operator's most
    recently undone action (any of UNDOABLE_ACTION_TYPES) that is still inside the
    window. Pressing it again steps forward through earlier undos, mirroring
    «Крок назад»."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if sync_control.is_paused():
        return _toast_response(_SYNC_PAUSED_MSG, kind="info")

    cutoff = datetime.utcnow() - timedelta(seconds=UNDO_WINDOW_SECONDS)
    entry = (
        db.query(ActionLog)
        .filter(
            ActionLog.operator_id == user.id,
            ActionLog.action_type.in_(UNDOABLE_ACTION_TYPES),
            ActionLog.undone_at.is_not(None),
            ActionLog.undone_at >= cutoff,
        )
        .order_by(ActionLog.undone_at.desc(), ActionLog.id.desc())
        .first()
    )
    if entry is None:
        return _toast_response("Немає що повторити", kind="info")

    return _perform_redo(db, user, entry)


# How many entries the «Останні дії» popup shows. Deliberately short: this is a
# "where was I just now" locator, not the audit log — /journal owns depth.
RECENT_ACTIONS_LIMIT = 10

# What the popup LISTS — a superset of what ← → can revert. Creating a work by
# hand is a real place the operator was and must be jumpable, but reverting it
# would mean deleting a row from the shared sheet, which stays behind the
# explicit delete button (with its confirm) rather than a one-click arrow.
# Excluded from both: the "undo"/"redo" bookkeeping rows, which would otherwise
# bury the actual edits every time the operator steps back.
RECENT_ACTION_TYPES = UNDOABLE_ACTION_TYPES + ("create",)


@app.get("/actions/recent", response_class=HTMLResponse)
def get_recent_actions(
    request: Request,
    tab: str = "",
    db: Session = Depends(get_db),
):
    """Render the «Останні дії» popup — this operator's last data-changing actions,
    newest first, each one clickable to jump to the work it touched.

    Scoped to the current operator on purpose: it reads the same ActionLog rows
    «Крок назад» steps through, so what the list shows and what the arrows can
    revert never disagree. Clicking an entry NEVER changes data — it only locates
    the row (see app.js) — so a stray click at the bench is harmless.

    ``tab`` is the day tab the queue is showing, used only to badge entries whose
    work lives on a different day (the jump then navigates instead of scrolling).
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    entries = db.execute(
        select(ActionLog)
        .options(selectinload(ActionLog.order))
        .where(
            ActionLog.operator_id == user.id,
            ActionLog.action_type.in_(RECENT_ACTION_TYPES),
        )
        .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
        .limit(RECENT_ACTIONS_LIMIT)
    ).scalars().all()

    return templates.TemplateResponse(
        request, "_actions_recent.html", {"entries": entries, "viewed_tab": tab.strip()}
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
    db.commit()

    # Запис коментаря в таблицю — best-effort і у ФОНІ: раніше це відкривало
    # Google прямо в потоці запиту й додавання зависало на час відповіді
    # (до ~40с холодним на лаб-проксі). Коментар у базі вже збережено; таблиця
    # наздоганяє, помилка йде в SyncLog.
    if order.sheet_tab and order.source == "lab":
        line = f"[{now:%d.%m.%Y %H:%M} · {author}] {clean_text}"
        _append_comment_background(order.id, comment.id, line)

    return RedirectResponse(f"/orders/{order.id}", status_code=303)


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

    # Archived work (out of the retention window or removed from Google) is a
    # historical record — the passport opens read-only so the operator reviews
    # it (Sum3D, timeline) without editing frozen history.
    read_only = _order_is_archived(order, date.today() - timedelta(days=RETENTION_DAYS))

    # Laconic action journal for THIS work (Sum3D/status/undo), newest first —
    # one line per operator action, the "хто що зробив" record.
    actions = db.execute(
        select(ActionLog)
        .where(ActionLog.order_id == order.id)
        .order_by(ActionLog.created_at.desc())
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "order": order,
            "user": user,
            "error": error,
            "statuses": STATUSES,
            "read_only": read_only,
            "actions": actions,
        },
    )


@app.get("/journal", response_class=HTMLResponse)
def get_journal(
    request: Request,
    operator: str = "",
    day: str = "",
    db: Session = Depends(get_db),
):
    """Лаконічний журнал дій оператора — усі змінні дії (Sum3D, статус, undo)
    стрічкою, з фільтром за оператором і днем. Прозорий для всієї команди
    (хто що зробив); показує останні дії, обмежені вікном, щоб не тягнути все."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    query = (
        select(ActionLog)
        .options(selectinload(ActionLog.order), selectinload(ActionLog.operator))
        .order_by(ActionLog.created_at.desc())
    )
    op_id = int(operator) if operator.isdigit() else None
    if op_id is not None:
        query = query.where(ActionLog.operator_id == op_id)

    day_value = ""
    if day:
        try:
            picked = datetime.strptime(day, "%Y-%m-%d")
            query = query.where(
                ActionLog.created_at >= picked,
                ActionLog.created_at < picked + timedelta(days=1),
            )
            day_value = day
        except ValueError:
            pass

    limit = 500
    entries = db.execute(query.limit(limit + 1)).scalars().all()
    truncated = len(entries) > limit
    entries = entries[:limit]

    operators = db.scalars(select(User).order_by(User.full_name, User.username)).all()

    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "user": user,
            "entries": entries,
            "operators": operators,
            "selected_operator": op_id,
            "selected_day": day_value,
            "truncated": truncated,
            "limit": limit,
        },
    )


# Архів («Хроніка») живе в app/routers/archive.py.
app.include_router(archive_router)


def _handout_context(request: Request, user, source: str, day: str, db: Session) -> dict:
    """Усе, що показує екран видачі.

    Винесено з роута, бо відмітка «знайдено» тепер підмінює список карток
    через HTMX і мусить будувати рівно те саме, що й повне відкриття екрана —
    інакше після галочки прогрес, кнопка «Видати N з M» чи позначка поточного
    клієнта розійшлися б із рештою сторінки."""
    if source not in HANDOUT_SOURCE_FILTERS:
        source = "all"

    today = date.today()
    eligible = _handout_eligible_orders(db, today)

    # Day chips (14.08, 15.08, …): every past day that still has unissued
    # client works. `day` narrows the whole screen to that one day — the
    # operator hands out one day's furnace output at a time.
    handout_days = _handout_day_options(eligible)
    selected_day = _handout_select_day(handout_days, day)
    if selected_day is not None:
        shown = [o for o in eligible if _parse_sheet_tab(o.sheet_tab) == selected_day]
        # Скільки робіт лишилось на інших днях — щоб замовчування «останній
        # день» ніколи не ховало хвіст мовчки.
        other_days_count = len(eligible) - len(shown)
        eligible = shown
    else:
        other_days_count = 0

    groups: dict[str, list[Order]] = {}
    for order in eligible:
        groups.setdefault(order.client_name, []).append(order)

    # Sheet order (day, then row position top-to-bottom), not DB insertion
    # order — the lab reads this as a rough readiness timeline (furnaces close
    # at different times through the day), so a client's own works AND the
    # card order itself both follow it, same as flipping through the table.
    for group_orders in groups.values():
        group_orders.sort(key=_sheet_order_key)

    # `export` — шара Synology через SMB, де ціну диктує КІЛЬКІСТЬ звернень.
    # Повний обхід дерева тут коштував 65с (бойовий лог 25.08.26) і сторінка
    # не відкривалась. Тепер обхід ЛІНИВИЙ:
    #   рівень 1 (імена тек клієнтів) — один запит, потрібен для нечіткого
    #       зіставлення «ім'я в таблиці ↔ назва теки»;
    #   глибина — тільки для клієнтів, що реально на цьому екрані.
    # Робота стала пропорційна показаному (10-20 клієнтів), а не вмісту
    # сховища (сотні тек).
    _export_root = Path(get_export_folder_path(db))
    _scan_started = time.monotonic()
    folder_names = list_export_client_names_cached(_export_root)
    _not_before = _handout_not_before(eligible)
    entries: list = []          # наповнюється нижче, після обходу
    # Every client on this screen gets a card (idempotent), so «Клієнти» and the
    # handout always show the same people and the folder binding is always one
    # click away. Cheap next to the export scan above.
    _ensure_client_profiles(db, eligible)
    clients_by_name = {
        c.canonical_name.strip().casefold(): c.id
        for c in db.scalars(select(Client)).all()
        if c.canonical_name
    }
    client_names = [c for c in clients_by_name]

    def _client_id_for(name: str) -> int | None:
        """Exact fold first, then the same fuzzy matcher — the sheet spells one
        lab several ways and they must all reach the one card."""
        folded = (name or "").strip().casefold()
        if folded in clients_by_name:
            return clients_by_name[folded]
        hit = match_client_name(folded, client_names, {}).matched_folder_name
        return clients_by_name.get(hit) if hit else None

    matches = _handout_client_matches(db, list(groups), folder_names)
    _folders = _matched_folders(matches)
    scanned = _scan_export_for_clients(_export_root, _folders, _not_before)
    # Тека прив'язана, а в вікні порожньо — значить файли скачали задовго до
    # фрезерування. Тоді дивимось найновіші партії клієнта без межі за датою:
    # це один scandir теки плюс захід у три найсвіжіші партії, а не повний
    # обхід усіх ~176. Бойовий випадок 28.08.26: «папку знайти не можу, хоча
    # вона є» (Светлана Криничко, робота 27.08, файли значно старіші).
    _empty = {name: folder for name, folder in _folders.items() if not scanned.get(name)}
    if _empty:
        for name, entries_ in _scan_export_latest_for_clients(_export_root, _empty).items():
            scanned[name] = entries_

    client_groups = []
    for client_name, group_orders in groups.items():
        match = matches[client_name]
        export_entries = scanned.get(client_name, [])
        entries.extend(export_entries)
        for entry in export_entries:
            entry.folder_uri = folder_to_file_uri(entry.folder_path)
            entry.preview_token = build_preview_token(
                entry.folder_path, {"export": get_export_folder_path(db)}
            )
        # Per-row candidates: narrow the client's export folders to the ones
        # whose material matches THIS work's material_color, oldest-first. The
        # path carries no наряд/Sum3D ID (user decision 16.08.26: export stays
        # "нова папка"), so this is an ASSIST, not an exact bind — when several
        # works share a material the same folders show under each, and the
        # operator picks by eye (Sum3D ID + STL preview are their anchor).
        for order in group_orders:
            order.export_matches = _entries_for_material(
                order.material_color, export_entries, _parse_sheet_tab(order.sheet_tab)
            )
        # Теки, чий матеріал не збігся з жодним рядком, раніше показувались
        # окремим підвалом «Інші папки». Прибрано на прохання оператора
        # (28.08.26): на ранковій видачі це шум — звіряють коронку з STL за
        # рядком роботи, а не гортають чужі теки. Тека клієнта цілком лишається
        # одним кліком (`client_folder_uri`), якщо матеріал таки підписали не
        # так і теку треба відкрити руками.
        all_found = all(o.status in ("знайдено при видачі", "видано") for o in group_orders)
        # Client-level folder (the parent of the material folders) so the client
        # name itself opens the right place on disk, and a link to the client
        # card so an unbound client can be fixed once instead of every morning.
        # Тека клієнта береться з самого ЗІСТАВЛЕННЯ, а не з знайдених партій.
        # Раніше вона залежала від `export_entries`, тож клієнт із прив'язаною
        # текою, але без свіжих партій, отримував заклик «Прив'язати папку» —
        # екран казав «не прив'язано» там, де насправді «немає свіжих партій»
        # (бойовий випадок 28.08.26).
        client_folder_uri = None
        client_folder_token = None
        if match.matched_folder_name:
            client_folder = _export_root / match.matched_folder_name
            client_folder_uri = folder_to_file_uri(client_folder)
            # Токен, а не лише file://-посилання: браузер МОВЧКИ блокує перехід
            # на file:// зі сторінки на http, тому кнопка «Відкрити папку» досі
            # не робила нічого (бойовий випадок 28.08.26). Провідник відкриває
            # сервер через /open-folder, як це вже роблять прев'ю і черга.
            client_folder_token = build_preview_token(
                client_folder, {"export": get_export_folder_path(db)}
            )
        client_groups.append(
            {
                "client_name": client_name,
                "orders": group_orders,
                "match": match,
                "export_entries": export_entries,
                "all_found": all_found,
                "client_folder_uri": client_folder_uri,
                "client_folder_token": client_folder_token,
                "client_id": _client_id_for(client_name),
            }
        )

    # Cards themselves follow the same top-to-bottom principle, keyed off
    # each client's earliest (already-sorted) work.
    client_groups.sort(key=lambda g: _sheet_order_key(g["orders"][0]))

    source_counts = count_client_groups_by_source(client_groups)
    client_groups = filter_client_groups_by_source(client_groups, source)
    handout_flash = request.session.pop("handout_flash", None)

    # Queue position + per-client totals. The position is THE anchor of the
    # screen: the operator works strictly in the order the clients were milled
    # (which is the sheet order these groups are already sorted by), so it is
    # numbered explicitly rather than left implicit in the scroll position.
    # `is_current` marks the first client not yet fully found — where the
    # operator is right now.
    current_marked = False
    for index, group in enumerate(client_groups, start=1):
        group["position"] = index
        group["works_count"] = len(group["orders"])
        group["units_total"] = sum(_quantity_units(o.quantity) for o in group["orders"])
        group["found_count"] = sum(
            1 for o in group["orders"] if o.status in ("знайдено при видачі", "видано")
        )
        group["is_current"] = not group["all_found"] and not current_marked
        if group["is_current"]:
            current_marked = True

    done_groups = sum(1 for g in client_groups if g["all_found"])
    # Clients with no bound export folder. Counted so the screen can say it ONCE
    # at the top instead of putting a warning chip on every card — with nothing
    # bound yet that was 34 amber calls-to-action, which reads as noise and
    # buries the one client the operator is actually on.
    unbound_count = sum(1 for g in client_groups if not g["client_folder_uri"])

    # Таймінг обходу сховища в лог: без нього причину «сторінка не
    # відкривається» доводиться вгадувати (так і сталось 27.08.26).
    logger.info(
        "Handout export scan: %d клієнтів на екрані, %d тек у сховищі, "
        "%d записів, партії від %s, %.2fс",
        len(groups), len(folder_names), len(entries),
        _not_before.date() if _not_before else "усі",
        time.monotonic() - _scan_started,
    )
    return {
            "page_title": "Ранкова видача",
            "user": user,
            "client_groups": client_groups,
            "source": source,
            "source_counts": source_counts,
            "handout_flash": handout_flash,
            "handout_days": [d.strftime("%d.%m.%y") for d in handout_days],
            "selected_day": selected_day.strftime("%d.%m.%y") if selected_day else "",
            # Те, що треба покласти в посилання/форми, щоб повернутись СЮДИ ж:
            # порожній рядок означав би «замовчування», а не «всі дні».
            "day_param": selected_day.strftime("%d.%m.%y") if selected_day else HANDOUT_ALL_DAYS,
            "other_days_count": other_days_count,
            "prev_day": _adjacent_handout_day(handout_days, selected_day, -1),
            "next_day": _adjacent_handout_day(handout_days, selected_day, +1),
            "day_window": _handout_day_window(handout_days, selected_day),
            "done_groups": done_groups,
            "total_groups": len(client_groups),
            "unbound_count": unbound_count,
    }


@app.get("/handout", response_class=HTMLResponse)
def get_handout(
    request: Request, source: str = "all", day: str = "", db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "handout.html", _handout_context(request, user, source, day, db)
    )


def _handout_cards_response(request: Request, user, source: str, day: str, db: Session):
    """Лише список карток — відповідь на HTMX-відмітку.

    Сторінка НЕ перезавантажується, тому екран лишається рівно там, де
    оператор його прокрутив. Раніше кожна галочка йшла звичайною формою з
    редіректом, і після кожної екран смикався на початок — на видачі, де
    йдуть списком згори вниз і клацають підряд, це збивало саме те, заради
    чого екран і робився."""
    return templates.TemplateResponse(
        request, "_handout_cards.html", _handout_context(request, user, source, day, db)
    )


HANDOUT_DAY_WINDOW = 3


def _handout_day_window(days: list, selected) -> list[dict]:
    """The few days shown in the pager, newest-last, with the selected one marked.

    A single date with ‹ › arrows hid where the operator was in the week; a wall
    of 30+ chips drowned the screen. Three days is the middle: the current day
    plus its neighbours, so stepping back a day is one click and the position is
    visible. Anchored on the selected day (or the newest one when the screen
    shows all days), and clamped so the window stays full at either end."""
    if not days:
        return []
    size = min(HANDOUT_DAY_WINDOW, len(days))
    anchor = days.index(selected) if selected in days else len(days) - 1
    start = max(0, min(anchor - size // 2, len(days) - size))
    return [
        {"value": d.strftime("%d.%m.%y"), "label": d.strftime("%d.%m"), "active": d == selected}
        for d in days[start:start + size]
    ]


def _adjacent_handout_day(days: list, selected, step: int) -> str:
    """Neighbouring day for the ‹ › pager, or "" at the ends. Replaces the wall
    of 30+ day chips: the operator hands out one day at a time and steps between
    them, so only the neighbours need to be one click away."""
    if not days:
        return ""
    if selected is None:
        # No day chosen: ‹ opens the newest day, › stays inert.
        return days[-1].strftime("%d.%m.%y") if step < 0 else ""
    try:
        index = days.index(selected)
    except ValueError:
        return ""
    target = index + step
    if 0 <= target < len(days):
        return days[target].strftime("%d.%m.%y")
    return ""


def _handout_back_url(source: str, day: str) -> str:
    """Rebuild the exact handout view (source tab + day chip) the operator was
    on, so a mark/unmark POST returns them there instead of the unfiltered
    all-days list."""
    params: list[str] = []
    if source and source in HANDOUT_SOURCE_FILTERS and source != "all":
        params.append(f"source={source}")
    if day:
        params.append(f"day={day}")
    return "/handout" + ("?" + "&".join(params) if params else "")


@app.post("/orders/{order_id}/mark-found")
async def mark_found(
    request: Request,
    order_id: int,
    source: str = Form("all"),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
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
    # Found = physically located → clear the sheet's blue "pending" fill to
    # white, per the lab's colour convention. У фоні: галочка має ставитись
    # миттєво, бо оператор клацає їх підряд.
    _set_client_row_fill_background(order.id, blue=False)
    # HTMX-клік підмінює лише список карток — сторінка не перезавантажується,
    # тож скрол лишається там, де оператор його поставив. Редірект лишається
    # для звичайної форми (без JS) і для прямих переходів.
    if request.headers.get("HX-Request"):
        return _handout_cards_response(request, user, source, day, db)
    return RedirectResponse(_handout_back_url(source, day), status_code=303)


@app.post("/orders/{order_id}/unmark-found")
async def unmark_found(
    request: Request,
    order_id: int,
    source: str = Form("all"),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
    """Undo an accidental "знайдено" click: revert the order to pending and
    repaint the sheet row blue so the next sync doesn't read the cleared fill
    as issued. Refuses to touch an already-issued ("видано") order."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status != "знайдено при видачі":
        return RedirectResponse(_handout_back_url(source, day), status_code=303)

    order.status = "нове"
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username)
    )
    db.commit()
    # Un-found = back to pending → repaint the blue fill so sheet state and
    # portal status stay consistent (a white fill + "нове" would otherwise be
    # read as issued on the next sync). Так само у фоні.
    _set_client_row_fill_background(order.id, blue=True)
    # HTMX-клік підмінює лише список карток — сторінка не перезавантажується,
    # тож скрол лишається там, де оператор його поставив. Редірект лишається
    # для звичайної форми (без JS) і для прямих переходів.
    if request.headers.get("HX-Request"):
        return _handout_cards_response(request, user, source, day, db)
    return RedirectResponse(_handout_back_url(source, day), status_code=303)


@app.post("/handout/issue-group")
async def issue_handout_group(
    request: Request,
    client_name: str = Form(...),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
    """One click on a handout card's "Видати" button closes the whole client
    group: every found-but-not-yet-issued order flips to "видано" (mirroring
    the single-order status route), and every sheet_client row's blue
    "pending" fill is cleared back to white in ONE batched sheet call — the
    counterpart of the lab's own manual "clear the blue = issued" convention.

    Re-derives the group server-side from client_name (never trusts a client
    id list from the form) and issues EXACTLY the orders already marked
    "знайдено при видачі"/"видано", leaving the rest of the card open.

    Часткова видача — норма процесу, не виняток (CLAUDE.md §2): цирконій іде
    через три пічки, які відкриваються ~9:00, в обід і під вечір, тож роботи
    одного клієнта фізично виходять у різний час. Раніше тут стояв gate
    all_found, і клієнт із 52 роботами не міг отримати «Видати» жодного разу
    за день — оператор мусив або чекати до вечора, або обходити портал через
    Google-таблицю."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    # Handout clears the blue "pending" fill in the sheet — a table write, so
    # it's refused while paused; the operator issues after resume.
    if sync_control.is_paused():
        request.session["toast_flash"] = {"message": _SYNC_PAUSED_MSG, "kind": "info"}
        return RedirectResponse(
            f"/handout?day={day}" if day else "/handout", status_code=303
        )

    today = date.today()
    candidates = db.scalars(
        select(Order).where(Order.client_name == client_name, Order.status != "видано")
    ).all()
    group_orders = [
        o for o in candidates
        if (d := _parse_sheet_tab(o.sheet_tab)) is not None and d < today
    ]
    # When the handout screen is filtered to one day (day chips), the card
    # the operator sees — and therefore what "Видати" closes — is that day's
    # works only; the client's other days stay open.
    selected_day = _parse_sheet_tab(day) if day else None
    back_url = f"/handout?day={day}" if day else "/handout"
    if selected_day is not None:
        group_orders = [
            o for o in group_orders if _parse_sheet_tab(o.sheet_tab) == selected_day
        ]
    if not group_orders:
        return RedirectResponse(back_url, status_code=303)
    # Видаємо рівно те, що оператор уже знайшов. Решта лишається в картці.
    group_orders = [
        o for o in group_orders if o.status in ("знайдено при видачі", "видано")
    ]
    if not group_orders:
        request.session["handout_flash"] = {
            "kind": "info",
            "message": "Нічого видавати: жодну роботу цього клієнта не позначено «знайдено».",
        }
        return RedirectResponse(back_url, status_code=303)

    actor = user.full_name or user.username
    sync_error: str | None = None
    clear_targets: list[tuple[str, int]] = []  # (sheet_tab, row_number)
    for order in group_orders:
        order.status = "видано"
        sheet_fields = apply_status_markers(order, "видано", actor=actor)
        db.add(
            StatusEvent(order_id=order.id, operator_id=user.id, status="видано", actor=user.username)
        )
        err = _write_sheet_fields(db, order, sheet_fields)
        sync_error = sync_error or err
        if order.source == "sheet_client" and order.sheet_tab and order.row_number is not None:
            clear_targets.append((order.sheet_tab, order.row_number))

    if clear_targets:
        try:
            spreadsheet = open_spreadsheet(db=db)
            rows_by_sheet_id: list[tuple[int, int]] = []
            for sheet_tab, row_number in clear_targets:
                worksheet = get_worksheet_by_name(spreadsheet, sheet_tab)
                if worksheet is not None:
                    rows_by_sheet_id.append((worksheet.id, row_number + HEADER_ROWS))
            clear_row_fills(spreadsheet, rows_by_sheet_id)
        except Exception as exc:  # noqa: BLE001 — never fail the видано status over this
            logger.exception("Failed to clear blue fill for handout group %r", client_name)
            sync_error = sync_error or str(exc)

    db.commit()

    if sync_error:
        request.session["handout_flash"] = {
            "kind": "error",
            "message": f"Статус видано, але запис у таблицю не пройшов: {sync_error}",
        }
    return RedirectResponse(back_url, status_code=303)


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


# Emails that have left the triage queue: accepted into the queue or rejected.
# The archive view keeps them visible so a processed letter is never lost — the
# operator can look back at what came in and, for a mistaken reject, restore it.
_ARCHIVE_STATUSES = ("прийнято", "відхилено")


@app.get("/mail", response_class=HTMLResponse)
def get_mail(
    request: Request,
    db: Session = Depends(get_db),
    synced: int | None = None,
    error: str | None = None,
    service: str = "all",
    view: str = "pending",
    partial: str | None = None,
    open: int | None = None,
    since: int | None = None,
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Validate independently — an unknown/stale value degrades to "all"
    # (show everything) rather than erroring, same pattern as the queue
    # screen's source/ready filters.
    if service not in SERVICE_TYPE_FILTERS:
        service = "all"
    if view not in ("pending", "filtered", "archive", "auto"):
        view = "pending"
    # Pop the flash only on a full-page render — the 15s poll (partial="list")
    # would otherwise consume it before the real navigation shows it.
    toast_flash = request.session.pop("toast_flash", None) if partial != "list" else None

    # Three views: pending = "нове" NOT stamped by a filter rule; filtered =
    # "нове" stamped (kept, never deleted — one click brings a letter back);
    # archive = accepted/rejected.
    if view == "archive":
        status_clause = EmailMessage.status.in_(_ARCHIVE_STATUSES)
    elif view == "filtered":
        status_clause = sa_and(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_not(None)
        )
    else:
        status_clause = sa_and(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_(None)
        )
    # STABLE ORDER (pending view). The list polls every 15s; without this a
    # letter arriving mid-glance inserted itself and pushed every row down
    # under the operator's cursor — the same hazard the handout screen has a
    # written rule against (CLAUDE.md §2, rule 1). `since` is a high-water mark
    # of EmailMessage.id captured at full page render and echoed back by the
    # poll: the refreshed list shows only letters at or below it, so rows never
    # move. Anything newer is counted and offered as an explicit «+N нових»
    # banner, which the operator clicks when they are ready — that click is a
    # full navigation, which mints a new watermark.
    list_clause = status_clause
    if since is not None and view == "pending":
        list_clause = sa_and(status_clause, EmailMessage.id <= since)
    emails = db.scalars(
        select(EmailMessage)
        .where(list_clause)
        .options(selectinload(EmailMessage.attachments))
        .order_by(
            EmailMessage.received_at.desc().nullslast(),
            EmailMessage.created_at.desc()
        )
    ).all()
    # How many pending letters are being held back from the frozen list.
    held_back_count = 0
    if since is not None and view == "pending":
        held_back_count = db.scalar(
            select(func.count()).select_from(EmailMessage).where(
                status_clause, EmailMessage.id > since
            )
        ) or 0

    # Top-level view counts (pending vs filtered vs archive) for the tabs.
    pending_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_(None)
        )
    ) or 0
    filtered_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове", EmailMessage.filter_category.is_not(None)
        )
    ) or 0
    archive_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status.in_(_ARCHIVE_STATUSES)
        )
    ) or 0
    sender_memories = list_sender_memories(db) if view == "auto" else []
    auto_count = db.scalar(
        select(func.count()).select_from(ClientSenderMemory).where(
            ClientSenderMemory.auto_accept.is_(True)
        )
    ) or 0

    # Pending letters no operator has opened yet — drives the animated
    # "unread by me" highlight and the accent count on the pending tab.
    unread_count = db.scalar(
        select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == "нове",
            EmailMessage.seen_at.is_(None),
            EmailMessage.filter_category.is_(None),
        )
    ) or 0

    # Watermark for the frozen list (see the `since` comment above). On a full
    # render it is the newest letter id in existence; the poll echoes it back
    # unchanged, so the visible set stays put until the operator asks for more.
    list_watermark = since if since is not None else (
        db.scalar(select(func.max(EmailMessage.id))) or 0
    )

    # Service-type chips only make sense for the pending triage list.
    service_counts = count_by_service_type(emails) if view == "pending" else None
    if view == "pending":
        emails = filter_emails_by_service_type(emails, service)
    attach_email_preview_tokens(emails, _mail_trusted_roots(db), _mail_preview_roots(db))

    # Filter rules — listed (and managed by the admin) on the filtered tab.
    filter_rules = (
        db.scalars(
            select(MailFilterRule).order_by(MailFilterRule.id.desc())
        ).all()
        if view == "filtered"
        else []
    )
    filter_categories = _mail_filter_categories(db)
    filter_category_rows = (
        db.scalars(
            select(MailFilterCategory).order_by(MailFilterCategory.id.asc())
        ).all()
        if view == "filtered"
        else []
    )

    # Learning nudge: a sender whose letters were rejected 2+ times and who has
    # no sender rule yet (enabled OR disabled — a disabled rule records "the
    # operator said no, don't ask again") gets a one-line suggestion banner on
    # the pending tab.
    filter_suggest = None
    if view == "pending":
        rejected_counts = db.execute(
            select(EmailMessage.from_address, func.count().label("cnt"))
            .where(
                EmailMessage.status == "відхилено",
                EmailMessage.from_address.is_not(None),
            )
            .group_by(EmailMessage.from_address)
            .having(func.count() >= 2)
            .order_by(func.count().desc())
        ).all()
        if rejected_counts:
            sender_patterns = {
                (r.pattern or "").strip().lower()
                for r in db.scalars(
                    select(MailFilterRule).where(MailFilterRule.kind == "sender")
                ).all()
            }
            for address, cnt in rejected_counts:
                if address.strip().lower() not in sender_patterns:
                    filter_suggest = {"address": address, "count": cnt}
                    break

    # The 15s triage poll asks for just the list wrapper (_mail_triage_list.html)
    # so new letters appear with the unread highlight without a full reload. The
    # fragment re-renders the same #mail-list-rows so its poll attrs persist.
    if partial == "list":
        return templates.TemplateResponse(
            request,
            "_mail_triage_list.html",
            {
                "emails": emails,
                "view": view,
                "service": service,
                "list_watermark": list_watermark,
                "held_back_count": held_back_count,
            },
        )

    # Pre-open a letter in the right-hand panel (used after a partial accept so
    # the operator lands back in the two-pane list with the letter already open,
    # not on the standalone card page). Only if it's in the list being shown.
    open_panel_html = None
    open_id = None
    if open is not None:
        open_email = next((e for e in emails if e.id == open), None)
        if open_email is not None:
            open_id = open
            open_panel_html = templates.env.get_template("_mail_detail_panel.html").render(
                _mail_panel_context(db, open_email, user)
            )

    return templates.TemplateResponse(
        request,
        "mail_triage.html",
        {
            "page_title": "Нові з пошти",
            "emails": emails,
            "open_panel_html": open_panel_html,
            "open_id": open_id,
            "toast_flash": toast_flash,
            "user": user,
            "synced": synced,
            "error": error,
            "service": service,
            "service_counts": service_counts,
            "view": view,
            "pending_count": pending_count,
            "filtered_count": filtered_count,
            "sender_memories": sender_memories,
            "auto_count": auto_count,
            "archive_count": archive_count,
            "unread_count": unread_count,
            "filter_rules": filter_rules,
            "filter_categories": filter_categories,
            "filter_category_rows": filter_category_rows,
            "filter_suggest": filter_suggest,
            # Адреса скриньки, яку моніторить система — показуємо в шапці, щоб
            # оператор бачив, звідки саме тягнуться листи (None → не налаштовано).
            "mailbox": get_imap_login(db),
            # «Скачувати всі вкладення» — той самий тоггл, що в налаштуваннях,
            # продубльований у шапці тріажу для швидкого доступу (адмін).
            "mail_download_all": get_mail_download_all(db),
            # Frozen-list state (see the `since` comment above).
            "list_watermark": list_watermark,
            "held_back_count": held_back_count,
        },
    )


@app.post("/mail/sync")
def sync_mail(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Own session, not the request's: on a watchdog timeout the hung fetch
    # thread still owns whatever session it was given (see
    # mail_sync_service._fetch_with_deadline), and get_db would otherwise
    # close the request session out from under that zombie. _run_sync_owned
    # closes the session itself only when the run actually finished.
    try:
        count = _run_mail_sync_owned_session(trigger="manual")
    except (MailSyncBusyError, MailSyncError) as exc:
        return RedirectResponse(f"/mail?error={quote(str(exc))}", status_code=303)

    return RedirectResponse(f"/mail?synced={count}", status_code=303)


@app.get("/mail/{email_id}", response_class=HTMLResponse)
def get_mail_detail(
    request: Request,
    email_id: int,
    error: str | None = None,
    panel: int = 0,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    # Opening the triage card clears the "unread by me" highlight for everyone
    # (shared seen state). Stamp once — later opens keep the original time.
    if email.seen_at is None:
        email.seen_at = datetime.now()
        db.commit()

    context = _mail_panel_context(db, email, user, error=error)

    # HTMX click from the triage list swaps just the detail into the right
    # column (panel=1, or any HX-Request); a plain navigation still gets the
    # standalone page — the shared _mail_detail_panel.html renders both.
    request_headers = getattr(request, "headers", None) or {}
    if panel or request_headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "_mail_detail_panel.html", context)

    return templates.TemplateResponse(request, "mail_detail.html", context)


def _email_partial_state(db: Session, email: EmailMessage) -> dict:
    """Multi-colour partial-accept state for a letter: files not yet accepted
    (still in the spool) and how many order batches were already taken from it.
    Drives the wizard's file picker and the «частково прийнято» badge."""
    unclaimed = [
        a for a in email.attachments
        if a.order_id is None and Path(a.saved_path).exists()
    ]
    accepted_batches = db.scalar(
        select(func.count()).select_from(Order).where(Order.source_email_id == email.id)
    ) or 0
    return {
        "unclaimed_attachments": unclaimed,
        "unclaimed_count": len(unclaimed),
        "accepted_batches": accepted_batches,
        "is_partial": accepted_batches > 0 and bool(unclaimed),
    }


def _mail_panel_context(db: Session, email: EmailMessage, user, **extra) -> dict:
    """Shared render context for the triage detail panel — wizard step 1 seed,
    material candidates and the whitelisted download links detected in the body.
    Reused by get_mail_detail (the fetch-link route renders just one row)."""
    attach_email_preview_tokens([email], _mail_trusted_roots(db), _mail_preview_roots(db))
    seed = (email.material_color_guess or "") or (email.subject or "")
    # Recurring client? Sender memory beats every guess for the name prefill.
    sender_hint = lookup_sender(db, email)
    context = {
        "email": email,
        "user": user,
        "error": None,
        "wizard_step": 1,
        "client_name": sender_hint.client_name if sender_hint else "",
        "sender_hint": sender_hint,
        "material_color": "",
        "kind": "",
        "quantity": "",
        "folder_pick": "",
        "folder_new": "",
        "material_folder": "",
        "material_cands": material_candidates(seed, _lab_material_colors(db)),
        "body_links": extract_download_links(email.body_text),
        "undownloaded_links": [
            dl for dl in extract_download_links(email.body_text)
            if (dl.file_id or dl.url) not in (
                set(json.loads(email.handled_link_refs)) if email.handled_link_refs else set()
            )
        ],
        "handled_link_refs": set(json.loads(email.handled_link_refs)) if email.handled_link_refs else set(),
        # Any ZIP/RAR still sitting among the attachments (auto-unpack failed or
        # is off) → offer the manual «Розпакувати» reserve button.
        "has_archive": any(is_archive(a.filename) for a in email.attachments),
        "staged_count": sum(1 for a in email.attachments if a.staged_to_export and a.order_id is None),
        "link_flash": None,
        # Admin-editable category names for the card's «У фільтр» select.
        "filter_categories": _mail_filter_categories(db),
        **_email_partial_state(db, email),
    }
    context.update(extra)
    return context


@app.post("/mail/{email_id}/fetch-link", response_class=HTMLResponse)
def fetch_email_link(
    request: Request,
    email_id: int,
    ref: str = Form(...),
    db: Session = Depends(get_db),
):
    """Download ONE whitelisted share link (identified by its Drive file id or
    ukr.net URL) into the email's mail-spool folder as an attachment, and return
    just that link's row with its new status (done / skip / error). Per-link so
    the operator sees each file's progress separately. Only whitelisted hosts are
    ever fetched — see app/link_attachments.py."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    link = next(
        (dl for dl in extract_download_links(email.body_text) if (dl.file_id or dl.url) == ref),
        None,
    )
    if link is None:
        return templates.TemplateResponse(
            request,
            "_mail_link_row.html",
            {"email": email, "link": LinkAttachment(kind="?", url=ref, display=ref),
             "link_status": "error", "link_message": "посилання не знайдено в листі"},
        )

    existing = frozenset(a.filename for a in email.attachments)
    status = message = result_name = None
    try:
        path = download_link(link, Path(MAIL_ATTACHMENTS_PATH) / email.uid, existing_names=existing)
    except LinkDownloadError as exc:
        status, message = "error", str(exc)
    except Exception:  # noqa: BLE001 — one bad link mustn't 500 the panel
        logger.exception("Link download failed for email %s: %s", email.id, link.url)
        host = (urlsplit(link.url).hostname or "сервер файлів")
        status, message = "error", f"немає з'єднання з {host} (інтернет / проксі?)"
    else:
        if path is None:
            status = "skip"
        else:
            attachment = Attachment(
                email_message_id=email.id,
                filename=path.name,
                saved_path=str(path),
                size_bytes=path.stat().st_size,
            )
            db.add(attachment)
            email.attachments_status = "ready"
            status, result_name = "done", path.name
    if status in ("done", "skip"):
        # Remember this link as handled so the «ще N за посиланням» count drops.
        handled = set(json.loads(email.handled_link_refs) if email.handled_link_refs else [])
        handled.add(ref)
        email.handled_link_refs = json.dumps(sorted(handled))
    db.commit()

    # Auto-unpack a freshly downloaded archive (client packed the STL in a
    # .zip/.rar). Best-effort; the extracted files show on the next panel load,
    # and a toast tells the operator it happened.
    toast = None
    if status == "done" and result_name and is_archive(result_name):
        db.refresh(email)
        try:
            extracted, extract_errors = extract_archive_attachments(db, email)
            if extracted or extract_errors:
                db.commit()
            if extracted:
                toast = {"message": f"Розпаковано {extracted} файл(ів) з архіву — оновіть картку", "kind": "success"}
            elif extract_errors:
                toast = {"message": "Архів: " + extract_errors[0], "kind": "error"}
        except Exception:  # noqa: BLE001 — extraction must not 500 the panel
            logger.exception("Archive extract failed for email %s", email.id)
            db.rollback()

    response = templates.TemplateResponse(
        request,
        "_mail_link_row.html",
        {"email": email, "link": link, "link_status": status,
         "link_message": message, "result_name": result_name},
    )
    # A downloaded file changes the attachment list AND the STL gallery, which
    # this row-only swap can't refresh — signal the panel to re-render (app.js
    # debounces so "download all" refreshes once).
    triggers = {}
    if toast is not None:
        triggers["toast"] = toast
    if status in ("done", "skip"):
        triggers["mailFilesChanged"] = True
    if triggers:
        response.headers["HX-Trigger"] = json.dumps(triggers)
    return response


@app.post("/mail/{email_id}/extract-archives", response_class=HTMLResponse)
def extract_mail_archives(request: Request, email_id: int, db: Session = Depends(get_db)):
    """Manual reserve for the auto-unpack: extract every ZIP/RAR attachment of
    the letter now, and re-render the triage detail so the STL files appear."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    try:
        extracted, extract_errors = extract_archive_attachments(db, email)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Manual archive extract failed for email %s", email.id)
        db.rollback()
        extracted, extract_errors = 0, ["не вдалося розпакувати"]

    db.refresh(email)
    context = _mail_panel_context(db, email, user)
    response = templates.TemplateResponse(request, "_mail_detail_panel.html", context)
    if extracted:
        toast = {"message": f"Розпаковано {extracted} файл(ів) з архіву", "kind": "success"}
    elif extract_errors:
        toast = {"message": "Архів: " + extract_errors[0], "kind": "error"}
    else:
        toast = {"message": "Архівів для розпакування немає", "kind": "info"}
    response.headers["HX-Trigger"] = json.dumps({"toast": toast})
    return response


def _lab_material_colors(db: Session) -> list[str]:
    """Distinct free-text material/colour strings the lab actually used in the
    sheet (source=="lab") — the reference list the accept wizard matches a
    client's mangled spelling against."""
    return sorted(
        {
            m
            for (m,) in db.execute(
                select(Order.material_color).where(
                    Order.source == "lab", Order.material_color.is_not(None)
                )
            ).all()
            if m and m.strip()
        }
    )


def _resolve_wizard_overrides(
    folder_pick: str, folder_new: str, material_folder: str
) -> tuple[str, str]:
    """Fold the step-2 directory controls into the two overrides
    save_attachments_to_export understands. A typed new folder name wins over
    the dropdown pick; an empty pick means "auto-resolve". Material subfolder is
    passed through as-is (empty -> derive from material_color)."""
    client_override = (folder_new or "").strip() or (folder_pick or "").strip()
    return client_override, (material_folder or "").strip()


@app.post("/mail/{email_id}/wizard", response_class=HTMLResponse)
def mail_wizard(
    request: Request,
    email_id: int,
    step: int = Form(1),
    client_name: str = Form(""),
    material_color: str = Form(""),
    kind: str = Form(""),
    quantity: str = Form(""),
    folder_pick: str = Form(""),
    folder_new: str = Form(""),
    material_folder: str = Form(""),
    attachment_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Render one step of the semi-automatic accept wizard (client+material →
    directory → confirm). Each Next/Back re-renders the shared _mail_wizard.html
    fragment with the accumulated values carried in hidden inputs; nothing is
    written until the final step POSTs to /mail/{id}/accept."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    step = max(1, min(3, step))
    known = _lab_material_colors(db)
    # Candidates from the operator's current material text, or the recognised
    # guess / subject on the very first render.
    seed = material_color.strip() or (email.material_color_guess or "") or (email.subject or "")
    candidates = material_candidates(seed, known)

    sender_hint = lookup_sender(db, email)
    # Step 1 opens with the remembered name when the operator hasn't typed one;
    # step 2 pre-selects the remembered folder (only if it still exists) when
    # no explicit pick/new-folder override was given.
    if step == 1 and not client_name.strip() and sender_hint:
        client_name = sender_hint.client_name
    if (
        step >= 2 and sender_hint and sender_hint.export_folder
        and not folder_pick.strip() and not folder_new.strip()
    ):
        export_root_probe = Path(get_export_folder_path(db))
        if sender_hint.export_folder in list_client_folders(export_root_probe):
            folder_pick = sender_hint.export_folder

    client_override, material_override = _resolve_wizard_overrides(
        folder_pick, folder_new, material_folder
    )

    ctx = {
        "email": email,
        "user": user,
        "wizard_step": step,
        "sender_hint": sender_hint,
        "client_name": client_name,
        "material_color": material_color,
        "kind": kind,
        "quantity": quantity,
        "folder_pick": folder_pick,
        "folder_new": folder_new,
        "material_folder": material_folder,
        "material_cands": candidates,
        "attachment_ids": attachment_ids,
        **_email_partial_state(db, email),
    }
    # Files that will move in THIS batch: the operator's selection, or all
    # unclaimed when nothing is ticked (single-colour default).
    selected_ids = set(attachment_ids)
    _batch = [a for a in ctx["unclaimed_attachments"] if a.id in selected_ids] if selected_ids else ctx["unclaimed_attachments"]
    ctx["batch_count"] = len(_batch)
    if step >= 2:
        export_root = Path(get_export_folder_path(db))
        ctx["preview"] = preview_export_target(
            export_root, client_name, material_color, client_override, material_override
        )
        ctx["existing_folders"] = list_client_folders(export_root)
        ctx["attachment_count"] = ctx["batch_count"]

    return templates.TemplateResponse(request, "_mail_wizard.html", ctx)


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


@app.post("/mail/{email_id}/download-attachments", response_class=HTMLResponse)
def download_email_attachments(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Pull a non-whitelisted letter's files on demand ("skipped" → "ready").
    Re-renders the detail panel so the STL/preview and accept wizard appear."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")
    try:
        download_attachments_now(db, email, Path(MAIL_ATTACHMENTS_PATH))
        db.commit()
    except Exception as exc:  # noqa: BLE001 — surface a friendly error, don't 500
        db.rollback()
        logger.exception("Manual attachment download failed for email %s", email.id)
        context = _mail_panel_context(db, email, user, error=f"Не вдалося скачати файли: {exc}")
        return templates.TemplateResponse(request, "_mail_detail_panel.html", context)
    context = _mail_panel_context(db, email, user)
    return templates.TemplateResponse(request, "_mail_detail_panel.html", context)


@app.post("/mail/senders/add")
def add_sender_auto(
    request: Request,
    email_address: str = Form(...),
    db: Session = Depends(get_db),
):
    """Manually add an email to the trusted auto-download list without waiting
    for a first acceptance. Creates a sender-memory row (client name = the
    address until the first real accept fills it in) with auto on. Idempotent —
    an existing key is just switched on."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    key = (email_address or "").strip().lower()
    if key:
        row = db.scalar(select(ClientSenderMemory).where(ClientSenderMemory.sender_key == key))
        if row is None:
            db.add(ClientSenderMemory(
                sender_key=key, client_name=email_address.strip(),
                export_folder=None, orders_count=0, auto_accept=True,
                last_seen_at=datetime.now(),
            ))
        else:
            row.auto_accept = True
        db.commit()
    return RedirectResponse("/mail?view=auto", status_code=303)


@app.post("/mail/senders/{memory_id}/auto")
def toggle_sender_auto(
    request: Request,
    memory_id: int,
    db: Session = Depends(get_db),
):
    """Flip a sender's trusted auto-accept flag (any operator). Trusting a
    sender means their future letters are accepted automatically when the
    guardrails pass; existing letters already in triage are untouched."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    row = db.get(ClientSenderMemory, memory_id)
    if row is None:
        raise HTTPException(status_code=404, detail="sender not found")
    row.auto_accept = not row.auto_accept
    db.commit()
    return RedirectResponse("/mail?view=auto", status_code=303)


@app.post("/mail/{email_id}/accept", response_class=HTMLResponse)
async def accept_email(
    request: Request,
    email_id: int,
    client_name: str = Form(...),
    material_color: str = Form(""),
    kind: str = Form(""),
    quantity: str = Form(""),
    folder_pick: str = Form(""),
    folder_new: str = Form(""),
    material_folder: str = Form(""),
    attachment_ids: list[int] = Form(default=[]),
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

    # Which dated tab does this order belong to? The lab often works a day or
    # two behind, so TODAY's tab may not exist yet — writing the placeholder to
    # "16.08.26" when the newest real tab is "15.08.26" silently drops the row
    # (get_worksheet_by_name returns None) and strands the order on a phantom
    # day. Resolve to the newest existing dated tab on or before today instead;
    # fall back to today's name only if the sheet is unreachable or has no dated
    # tab, preserving the old behaviour in that edge case. The resolved
    # worksheet is reused for the write-back below (one fewer tab fetch).
    today = date.today()
    target_tab = today.strftime("%d.%m.%y")
    target_worksheet = None
    try:
        target_worksheet = latest_worksheet_on_or_before(open_spreadsheet(db=db), today)
        if target_worksheet is not None:
            target_tab = target_worksheet.title
    except Exception as exc:  # noqa: BLE001 — sheet trouble must not block accept
        logger.warning("Could not resolve target sheet tab for email %s: %s", email.id, exc)

    new_order = Order(
        source="email",
        # Real наряд identifier from the sheet — email orders never get one,
        # but sheet_tab uses the same "%d.%m.%y" shape table tabs use, so period
        # tabs, is_overdue() and folder lookups treat a priced mail order exactly
        # like one entered from the sheet (CLAUDE.md: an operator wants to find
        # yesterday's mail-sourced job the same way they'd find a table one).
        # row_number stays None on purpose — that's the real signal (source ==
        # "lab" too) that stops sheet write-back.
        sheet_tab=target_tab,
        row_number=None,
        client_name=client_name.strip() or None,
        material_color=material_color.strip() or None,
        kind=kind.strip() or None,
        quantity=quantity.strip() or None,
        status="нове",
    )
    ensure_seeded(db)
    new_order.material_id = resolve_material_id(
        new_order.material_color, load_alias_rows(db), material_id_by_name(db)
    )
    new_order.source_email_id = email.id
    db.add(new_order)
    db.flush()

    email.order_id = new_order.id
    db.add(
        StatusEvent(order_id=new_order.id, operator_id=user.id, status="нове", actor=user.username)
    )

    # Partial accept: only the files the operator selected for THIS colour move
    # now (a multi-colour letter is accepted in batches). "Unclaimed" = files
    # not yet moved by a previous batch (order_id is None). An empty selection
    # means "all remaining", the single-colour default.
    unclaimed = [
        a for a in email.attachments
        if a.order_id is None and Path(a.saved_path).exists()
    ]
    selected_ids = set(attachment_ids)
    attachments = [a for a in unclaimed if a.id in selected_ids] if selected_ids else unclaimed
    if not attachments:
        # Nothing to move, but the sender→client link is still worth keeping.
        remember_sender(db, email, new_order.client_name or "", None)
    if attachments:
        try:
            export_root = Path(get_export_folder_path(db))
            # Files already auto-staged into export (trusted-sender auto-download)
            # must NOT be moved again — only linked to this order. The rest move
            # from the spool as usual.
            to_move = [a for a in attachments if not a.staged_to_export]
            staged = [a for a in attachments if a.staged_to_export]
            used_folder = None
            if to_move:
                client_override, material_override = _resolve_wizard_overrides(
                    folder_pick, folder_new, material_folder
                )
                new_paths = save_attachments_to_export(
                    export_root,
                    new_order.client_name or "",
                    new_order.material_color or "",
                    [Path(a.saved_path) for a in to_move],
                    client_folder_override=client_override,
                    material_folder_override=material_override,
                )
                # Файли переїхали — кеш обходу export більше не відповідає диску.
                clear_export_cache()
                for attachment, new_path in zip(to_move, new_paths):
                    attachment.saved_path = str(new_path)
                    attachment.order_id = new_order.id
                try:
                    used_folder = new_paths[0].relative_to(export_root).parts[0] if new_paths else None
                except (ValueError, IndexError):
                    used_folder = None
            for attachment in staged:
                attachment.order_id = new_order.id
            if used_folder is None and staged:
                try:
                    used_folder = Path(staged[0].saved_path).relative_to(export_root).parts[0]
                except (ValueError, IndexError):
                    used_folder = None
            db.add(SyncLog(direction="mail_to_export", status="ok", message=f"email {email.id}: {len(attachments)} файл(ів)"))
            remember_sender(db, email, new_order.client_name or "", used_folder)
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
        # Reuse the tab resolved above (newest dated tab ≤ today). None means
        # the sheet was unreachable or has no dated tab — log and skip, exactly
        # as the old "tab not found" branch did.
        worksheet = target_worksheet
        if worksheet is None:
            db.add(
                SyncLog(
                    direction="mail_to_sheet",
                    sheet_tab=new_order.sheet_tab,
                    status="error",
                    message=(
                        f"email {email.id}: доступної датованої вкладки немає, "
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
            # Link the order to the row we just wrote. Without this, the next
            # sheet sync re-imports that наряд-less row as a SEPARATE
            # source="sheet_client" order — the same work would then appear
            # twice (once as "Пошта", once as "Клієнт"). With the row_number set,
            # sync matches it to this order and updates in place instead.
            new_order.row_number = note_row - HEADER_ROWS
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

    # Partial vs full acceptance: if the letter still holds unclaimed files
    # (another colour the operator hasn't accepted yet), keep it "нове" so it
    # stays in triage to be finished; otherwise it's fully accepted.
    remaining = [
        a for a in email.attachments
        if a.order_id is None and Path(a.saved_path).exists()
    ]
    email.status = "нове" if remaining else "прийнято"
    db.commit()

    # A truthful outcome toast, shown on the page we land on (session flash →
    # base.html). Reports exactly what happened: how many files were saved this
    # batch and, for a multi-colour letter, how many still wait in the letter.
    saved = len(attachments)
    mat = (new_order.material_color or "").strip() or "без матеріалу"
    if remaining:
        message = (
            f"Прийнято партію «{mat}»: збережено {saved} файл(ів). "
            f"Лишилось {len(remaining)} файл(ів) у листі — прийміть наступний колір."
        )
        kind = "success"
    elif saved:
        message = f"Роботу «{mat}» прийнято в чергу: збережено {saved} файл(ів)."
        kind = "success"
    else:
        message = f"Роботу «{mat}» прийнято в чергу без файлів (файлів не знайдено)."
        kind = "warning"
    request.session["toast_flash"] = {"kind": kind, "message": message}

    # Where to land: STAY IN TRIAGE either way. Still files left → back to this
    # letter to accept the next colour; fully done → the triage list, so the
    # operator keeps their place and the next letter is one click away. (This
    # used to redirect to the client queue when finished, which ejected the
    # operator from the screen on every completed letter and made them navigate
    # back — the reward for finishing was losing your place. The toast already
    # links the created order.) The wizard posts over HTMX, so a 303 would swap
    # page HTML into the panel — HX-Redirect drives a real navigation.
    target = f"/mail?open={email.id}" if remaining else "/mail"
    request_headers = getattr(request, "headers", None) or {}
    if request_headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


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

    staged = [a for a in email.attachments if a.staged_to_export and a.order_id is None]
    if staged:
        try:
            new_paths = restore_attachments_to_spool(
                Path(MAIL_ATTACHMENTS_PATH), email.uid, [Path(a.saved_path) for a in staged]
            )
            # Файли переїхали — кеш обходу export більше не відповідає диску.
            clear_export_cache()
            for attachment, new_path in zip(staged, new_paths):
                attachment.saved_path = str(new_path)
                attachment.staged_to_export = False
        except (OSError, ValueError):
            logger.exception("Could not return auto-staged files to spool for email %s", email.id)

    email.status = "відхилено"
    db.commit()

    # Two callers: the triage LIST row (HTMX, hx-swap="delete" — wants just that
    # one row gone) and the detail card's plain form (full navigation). For the
    # HTMX case return an empty 200 so htmx deletes only the target row; a 303
    # to the full /mail page would be followed and its whole-page body fed to the
    # delete swap, which (with the polled #mail-list-rows wrapper + hx-preserve)
    # wiped the entire list. 204 is unusable here — htmx skips the swap on 204.
    request_headers = getattr(request, "headers", None) or {}
    if request_headers.get("HX-Request") == "true":
        return HTMLResponse("", status_code=200)
    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/{email_id}/unfilter")
def unfilter_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Bring a rule-filtered letter back to the main triage list. Clearing the
    stamp is the whole undo — and apply_filters_to_email never re-stamps an
    already-processed letter, so the operator's decision sticks."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    email.filter_category = None
    email.filter_rule_id = None
    db.commit()

    # HX = the «↩» on a filtered-list row (delete just that row); plain POST =
    # the card's «Повернути з фільтра» → land on the pending list, where the
    # returned letter now lives.
    request_headers = getattr(request, "headers", None) or {}
    if request_headers.get("HX-Request") == "true":
        return HTMLResponse("", status_code=200)
    return RedirectResponse("/mail", status_code=303)


_DEFAULT_FILTER_CATEGORIES = ["3D-друк", "бухгалтерія", "спам", "інше"]


def _mail_filter_categories(db: Session) -> list[str]:
    """Admin-editable category names (settings screen), falling back to the
    four defaults if the table is somehow empty — the selects must never render
    without options."""
    names = db.scalars(
        select(MailFilterCategory.name).order_by(MailFilterCategory.id.asc())
    ).all()
    return list(names) or list(_DEFAULT_FILTER_CATEGORIES)


def _filters_return_url(return_to: str) -> str:
    """Where a filter-rule/category action lands: the settings section when the
    form lives there, the filtered tab otherwise."""
    return "/settings#mail-filters" if return_to == "settings" else "/mail?view=filtered"


@app.post("/mail/{email_id}/filter")
def filter_email_manually(
    request: Request,
    email_id: int,
    category: str = Form("інше"),
    db: Session = Depends(get_db),
):
    """Manually move ONE letter to the «Відфільтровані» tab — no rule is
    created, nothing else is affected. The stamp has no rule FK, so the letter
    reads "filtered by hand"; «↩» brings it back like any other."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    email.filter_category = (category or "").strip() or "інше"
    email.filter_rule_id = None
    db.commit()
    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/filters")
def create_mail_filter(
    request: Request,
    kind: str = Form(...),
    pattern: str = Form(...),
    category: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a triage filter rule (admin) and apply it retroactively to the
    letters currently in the pending list — the reason the admin is creating it
    is usually a letter they're looking at right now."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    kind = kind.strip()
    pattern = pattern.strip()
    category = category.strip()
    if kind not in ("keyword", "sender") or not pattern or not category:
        return RedirectResponse(
            f"/mail?view=filtered&error={quote('Правило: вкажіть тип, шаблон і категорію')}",
            status_code=303,
        )

    rule = MailFilterRule(
        kind=kind, pattern=pattern, category=category,
        created_by=user.username,
    )
    db.add(rule)
    db.flush()
    apply_rule_retroactively(db, rule)
    db.commit()

    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@app.post("/mail/filters/{rule_id}/edit")
def edit_mail_filter(
    request: Request,
    rule_id: int,
    kind: str = Form(...),
    pattern: str = Form(...),
    category: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Edit a rule in place (admin) — no more delete-and-recreate. Letters the
    OLD version already stamped keep their stamp (history); the edited rule is
    re-applied retroactively so a broadened pattern catches pending letters
    right away."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    rule = db.get(MailFilterRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")

    kind = kind.strip()
    pattern = pattern.strip()
    category = category.strip()
    if kind not in ("keyword", "sender") or not pattern or not category:
        return RedirectResponse(
            f"{_filters_return_url(return_to)}&error={quote('Правило: вкажіть тип, шаблон і категорію')}"
            if return_to != "settings"
            else _filters_return_url(return_to),
            status_code=303,
        )

    rule.kind = kind
    rule.pattern = pattern
    rule.category = category
    apply_rule_retroactively(db, rule)
    db.commit()
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@app.post("/mail/filter-categories")
def create_filter_category(
    request: Request,
    name: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    name = name.strip()
    if name and not db.scalar(
        select(MailFilterCategory).where(func.lower(MailFilterCategory.name) == name.lower())
    ):
        db.add(MailFilterCategory(name=name))
        db.commit()
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@app.post("/mail/filter-categories/{category_id}/rename")
def rename_filter_category(
    request: Request,
    category_id: int,
    name: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Rename a category (admin). Cascades into existing rules AND stamped
    letters so the badge language stays consistent everywhere."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    cat = db.get(MailFilterCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="category not found")
    new_name = name.strip()
    if new_name and new_name != cat.name:
        old_name = cat.name
        cat.name = new_name
        db.execute(
            sa_update(MailFilterRule)
            .where(MailFilterRule.category == old_name)
            .values(category=new_name)
        )
        db.execute(
            sa_update(EmailMessage)
            .where(EmailMessage.filter_category == old_name)
            .values(filter_category=new_name)
        )
        db.commit()
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@app.post("/mail/filter-categories/{category_id}/delete")
def delete_filter_category(
    request: Request,
    category_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Delete a category (admin) — refused while any rule still uses it (edit
    those rules first). Stamped letters keep the old string as history and
    never block."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    cat = db.get(MailFilterCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="category not found")
    in_use = db.scalar(
        select(func.count()).select_from(MailFilterRule).where(
            MailFilterRule.category == cat.name
        )
    ) or 0
    if in_use:
        target = _filters_return_url(return_to)
        sep = "&" if "?" in target else "?"
        return RedirectResponse(
            f"{target}{sep}error={quote('Категорію використовують правила — спершу змініть їх')}"
            if return_to != "settings" else target,
            status_code=303,
        )
    db.delete(cat)
    db.commit()
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@app.post("/mail/filters/dismiss-suggest")
def dismiss_filter_suggest(
    request: Request,
    address: str = Form(...),
    db: Session = Depends(get_db),
):
    """«Ні» on the suggestion banner: record the refusal as a DISABLED sender
    rule so the banner never nags about this sender again. Costs nothing — a
    disabled rule filters nothing and can be enabled later from the rules
    panel if the operator changes their mind."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    address = address.strip()
    if address:
        db.add(
            MailFilterRule(
                kind="sender", pattern=address, category="відхилені",
                enabled=False, created_by=user.username,
            )
        )
        db.commit()
    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/filters/{rule_id}/toggle")
def toggle_mail_filter(
    request: Request,
    rule_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Enable/disable a rule (admin). Disabling never un-stamps already
    filtered letters — those return via each letter's own «Повернути» button,
    keeping the two decisions independent and predictable."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    rule = db.get(MailFilterRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    rule.enabled = not rule.enabled
    db.commit()
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


@app.post("/mail/filters/{rule_id}/delete")
def delete_mail_filter(
    request: Request,
    rule_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Delete a rule (admin). Letters it filtered keep their category badge
    (historical fact) but lose the FK; they stay on the filtered tab until an
    operator returns them."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    rule = db.get(MailFilterRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    db.execute(
        sa_update(EmailMessage)
        .where(EmailMessage.filter_rule_id == rule.id)
        .values(filter_rule_id=None)
    )
    db.delete(rule)
    db.commit()
    return RedirectResponse(_filters_return_url(return_to), status_code=303)


def _unaccept_email(db: Session, email: EmailMessage) -> None:
    """Fully undo EVERY order accepted from this letter (a multi-colour letter
    can have several), returning it to the pre-accept "нове" state: move all
    claimed attachments from export back to the mail spool, blank each order's
    sheet placeholder row, and delete the orders. Raises on a filesystem error
    (the move has its own rollback) so the caller can abort cleanly; sheet
    blanking is best-effort. Side effects first, DB mutations last."""
    orders = db.scalars(
        select(Order).where(Order.source_email_id == email.id)
    ).all()
    # Legacy safety net: pre-0012 accepts linked only via email.order_id.
    if not orders and email.order_id:
        legacy = db.get(Order, email.order_id)
        if legacy is not None:
            orders = [legacy]

    attachments = list(email.attachments)
    if attachments:
        new_paths = restore_attachments_to_spool(
            Path(MAIL_ATTACHMENTS_PATH), email.uid, [Path(a.saved_path) for a in attachments]
        )
        # Файли переїхали — кеш обходу export більше не відповідає диску.
        clear_export_cache()
        for attachment, new_path in zip(attachments, new_paths):
            attachment.saved_path = str(new_path)
            attachment.order_id = None
            attachment.staged_to_export = False

    spreadsheet = None
    for order in orders:
        if order.sheet_tab and order.row_number is not None:
            try:
                if spreadsheet is None:
                    spreadsheet = open_spreadsheet(db=db)
                worksheet = get_worksheet_by_name(spreadsheet, order.sheet_tab)
                if worksheet is not None:
                    clear_placeholder_row(worksheet, order.row_number + HEADER_ROWS)
            except Exception:  # noqa: BLE001 — sheet cleanup must not block the undo
                logger.exception("Could not blank sheet placeholder row for email %s", email.id)

    for order in orders:
        db.delete(order)
    email.order_id = None
    email.status = "нове"
    email.attachments_status = "ready"


@app.post("/mail/{email_id}/restore")
async def restore_email(
    request: Request,
    email_id: int,
    db: Session = Depends(get_db),
):
    """Return a processed email to the triage queue (status → "нове"). A rejected
    letter just flips status (its files never left the spool). An ACCEPTED letter
    is fully un-accepted: attachments move back from export, the sheet placeholder
    row is blanked and the created Order is deleted, so re-processing can't leave
    a duplicate."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="email not found")

    has_orders = bool(
        db.scalar(select(func.count()).select_from(Order).where(Order.source_email_id == email.id))
        or email.order_id
    )
    if email.status == "відхилено":
        email.status = "нове"
        db.commit()
        request.session["toast_flash"] = {"kind": "success", "message": "Лист повернуто в «Усі листи»."}
    elif email.status == "прийнято" or has_orders:
        # "прийнято" = fully accepted; a "нове" letter WITH orders = partially
        # accepted (some colours taken, more remain). Either way, undo every
        # order and put all files back — a clean restart of the whole letter.
        try:
            _unaccept_email(db, email)
            db.commit()
        except (OSError, ValueError) as exc:
            db.rollback()
            return RedirectResponse(
                f"/mail?view=archive&error={quote('Не вдалося відкотити прийняття: ' + str(exc))}",
                status_code=303,
            )
        request.session["toast_flash"] = {
            "kind": "success",
            "message": "Прийняття відкочено: роботи видалено, файли повернуто в лист.",
        }

    return RedirectResponse("/mail", status_code=303)
