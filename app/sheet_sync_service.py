"""Application service for importing dated Google Sheets tabs into the DB."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import re
from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, SyncLog
from app.parser import parse_rows
from app.sheet_colors import fetch_row_fills
from app.settings_store import get_google_service_account_json, get_google_sheet_id
from app.sheets import (
    call_with_retry,
    get_worksheet_by_name,
    open_spreadsheet,
    tab_name_for,
)
from app.sync import sync_tab


_DATE_TAB_RE = re.compile(r"^\d{2}\.\d{2}\.\d{2}$")
_INITIAL_LOOKBACK_DAYS = 30


class SheetSyncError(RuntimeError):
    """Safe, user-displayable Google Sheets synchronization error."""


class SheetSyncConfigurationError(SheetSyncError):
    """Raised when required Google Sheets settings are missing or invalid."""


class SheetSyncBusyError(SheetSyncError):
    """Raised when another manual/background synchronization owns the lock."""


_sync_lock = Lock()
# How long a MANUAL sync waits for the lock before giving up — long enough to
# outlast one hot-tab tick (~3s warm), short enough that a click during a real
# full sync still errors out promptly instead of hanging the request.
_MANUAL_LOCK_WAIT_SECONDS = 10.0


@dataclass
class SheetSyncSummary:
    tabs_processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    rows_seen: int = 0
    tab_names: list[str] = field(default_factory=list)


def _parse_tab_date(title: str) -> date | None:
    if not _DATE_TAB_RE.fullmatch(title):
        return None
    try:
        return datetime.strptime(title, "%d.%m.%y").date()
    except ValueError:
        return None


def _worksheets_to_sync(
    session: Session,
    spreadsheet,
    today: date,
    include_tabs: set[str] | None = None,
    full_history: bool = False,
) -> tuple[list, set[str]]:
    """Choose a bounded initial history, then the operational three-day window.

    Returns ``(worksheets_to_import, all_dated_tab_titles)`` — the second set
    is EVERY dated tab currently present in the document (imported or not),
    used by sync_google_sheets to delete orders orphaned by a whole tab being
    removed from the sheet.

    ``include_tabs`` force-adds specific tab titles (dd.mm.yy) even when they
    fall outside that window — used so a manual sync of a day the operator is
    actually looking at re-reads that older tab and can reconcile deletions
    there (a periodic run never revisits old tabs, for proxy-speed reasons).

    ``full_history`` imports EVERY dated tab in the document regardless of the
    window — a one-off "pull the whole sheet" the operator triggers explicitly
    (Settings → «Імпортувати всю історію»). It is intentionally NOT the periodic
    path: a background tick still only touches today±1 for proxy speed, and the
    reconciliation below never deletes already-imported old tabs (their titles
    stay in all_dated_titles), so history imported once persists cheaply."""
    include_tabs = include_tabs or set()
    has_sheet_orders = session.scalar(
        select(func.count(Order.id)).where(Order.source == "lab")
    ) > 0
    first_day = today - timedelta(days=1 if has_sheet_orders else _INITIAL_LOOKBACK_DAYS)
    last_day = today + timedelta(days=1)

    dated = []
    all_dated_titles: set[str] = set()
    for worksheet in call_with_retry(spreadsheet.worksheets):
        tab_date = _parse_tab_date(worksheet.title)
        if tab_date is None:
            continue
        all_dated_titles.add(worksheet.title)
        if (
            full_history
            or (first_day <= tab_date <= last_day)
            or worksheet.title in include_tabs
        ):
            dated.append((tab_date, worksheet.title, worksheet))
    dated.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in dated], all_dated_titles


def _configuration(session: Session) -> tuple[str, str]:
    sheet_id = (get_google_sheet_id(session) or "").strip()
    credentials_json = (get_google_service_account_json(session) or "").strip()
    if not sheet_id:
        raise SheetSyncConfigurationError(
            "У налаштуваннях не вказано Google Sheet ID."
        )
    if not credentials_json:
        raise SheetSyncConfigurationError(
            "У налаштуваннях не додано JSON сервісного акаунта Google."
        )
    try:
        credentials = json.loads(credentials_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SheetSyncConfigurationError(
            "JSON сервісного акаунта Google має некоректний формат."
        ) from exc
    if not isinstance(credentials, dict):
        raise SheetSyncConfigurationError(
            "JSON сервісного акаунта Google має некоректний формат."
        )
    return sheet_id, credentials_json


def _safe_failure(exc: Exception) -> SheetSyncError:
    if isinstance(exc, SheetSyncError):
        return exc
    return SheetSyncError(
        "Не вдалося синхронізувати Google Таблицю. Перевірте доступ сервісного "
        "акаунта, Sheet ID та підключення до інтернету."
    )


def _record_failure(
    session: Session, tab_name: str | None, error: SheetSyncError, *, persist: bool
) -> None:
    session.rollback()
    if not persist:
        return
    session.add(
        SyncLog(
            direction="sheet_to_db",
            sheet_tab=tab_name,
            status="error",
            # Only our controlled, user-safe message is persisted. Credential
            # contents and raw third-party exception text never enter SyncLog.
            message=str(error),
        )
    )
    try:
        session.commit()
    except Exception:
        session.rollback()


def sync_google_sheets(
    session: Session,
    *,
    trigger: str = "manual",
    include_tabs: set[str] | None = None,
    full_history: bool = False,
) -> SheetSyncSummary:
    """Import relevant dated tabs tab-by-tab and persist an audit log.

    The first import covers the most recent 30 days plus tomorrow. Later runs
    cover yesterday, today and tomorrow.

    Each tab is committed on its own: a failure part-way through a multi-tab
    import (say tab 25 of 30) rolls back ONLY the failing tab and preserves the
    tabs already imported, instead of discarding the whole run. Retries
    (call_with_retry) absorb transient blips first, so a failure that still
    reaches here is treated as fatal for this run: the failing tab is rolled
    back, its sanitized error is logged, and processing stops rather than
    hammering an unhealthy connection through the remaining tabs. A setup
    failure (missing config, can't open/list the spreadsheet) imports nothing.

    The process-wide non-blocking lock prevents a background run and a button
    click (or two overlapping background ticks) from importing the same tab
    concurrently — same pattern as app/mail_sync_service.py. ``trigger`` is
    audit metadata (``manual`` or ``background``): background runs that find
    nothing new skip the SyncLog write entirely, and background failures are
    not persisted (only logged), so a prolonged outage doesn't fill the audit
    table with a row every couple of minutes.
    """
    if trigger not in {"manual", "background"}:
        raise ValueError("unsupported sheet sync trigger")
    # A manual click waits out a hot-tab tick (~3s every 15s would otherwise
    # give the button a ~20% chance of a spurious "вже виконується"); the
    # background full sync stays non-blocking — its worker thread never
    # overlaps the hot lane anyway, so a busy lock there means a manual run
    # is in flight and this tick can just skip.
    if trigger == "manual":
        acquired = _sync_lock.acquire(timeout=_MANUAL_LOCK_WAIT_SECONDS)
    else:
        acquired = _sync_lock.acquire(blocking=False)
    if not acquired:
        raise SheetSyncBusyError(
            "Синхронізація Google Таблиці вже виконується. Спробуйте трохи пізніше."
        )

    summary = SheetSyncSummary()
    try:
        try:
            _configuration(session)
            spreadsheet = open_spreadsheet(db=session)  # retries internally
            worksheets, all_dated_titles = _worksheets_to_sync(
                session, spreadsheet, date.today(), include_tabs, full_history=full_history
            )
        except Exception as exc:
            # Setup failure: nothing has been imported, so there is no partial
            # progress to preserve — surface it and record it like before.
            safe_error = _safe_failure(exc)
            _record_failure(session, None, safe_error, persist=trigger == "manual")
            raise safe_error from exc

        for worksheet in worksheets:
            current_tab = worksheet.title
            try:
                raw = call_with_retry(worksheet.get_all_values)
                rows = parse_rows(raw)
                # Read fill colours (best-effort) so client rows whose blue was
                # cleared flip to "видано" and grey SLM rows are filtered out.
                row_fills = fetch_row_fills(worksheet)
                result = sync_tab(
                    session, current_tab, rows,
                    row_fills=row_fills, raw_row_count=len(raw),
                )
                # Commit this tab before touching the next, so a later tab's
                # failure can never undo it.
                session.commit()
            except Exception as exc:
                safe_error = _safe_failure(exc)
                _record_failure(session, current_tab, safe_error, persist=trigger == "manual")
                raise safe_error from exc

            summary.tabs_processed += 1
            summary.tab_names.append(current_tab)
            summary.rows_seen += len(rows)
            summary.created += result.created
            summary.updated += result.updated
            summary.unchanged += result.unchanged
            summary.deleted += result.deleted

        # Orders orphaned by a WHOLE tab deleted from the sheet: the per-tab
        # reconciliation above only sees rows inside tabs that still exist, so
        # an order whose dated tab vanished would linger forever (and keep its
        # phantom day in the queue's day-strip). Deleting a tab in the sheet
        # means "this day's records are gone" — mirror that here for
        # sheet-sourced orders only; email orders are stamped with a business
        # date, not a real tab, and are never touched.
        if all_dated_titles:
            orphans = [
                o
                for o in session.scalars(
                    select(Order).where(
                        Order.source.in_(("lab", "sheet_client")),
                        Order.sheet_tab.isnot(None),
                        Order.sheet_tab.notin_(all_dated_titles),
                        # Only ones still active — an already-archived order must
                        # not be re-stamped (and re-logged) on every full sync.
                        Order.archived_at.is_(None),
                    )
                )
                # Only orders whose sheet_tab actually names a dated tab: a
                # non-dd.mm.yy value was never a real sheet tab, so its absence
                # from the listing proves nothing.
                if _parse_tab_date(o.sheet_tab) is not None
            ]
            # Keep, don't delete: a whole tab removed from the sheet (the lab
            # prunes old days for space) archives its orders instead of wiping
            # them — they leave the working queue but stay findable in the
            # Archive. Email orders and non-dated sheet_tab values are untouched.
            archived_at = datetime.utcnow()
            for orphan in orphans:
                orphan.archived_at = archived_at
                summary.deleted += 1
            if orphans:
                gone_tabs = sorted({o.sheet_tab for o in orphans})
                session.add(
                    SyncLog(
                        direction="sheet_to_db",
                        status="ok",
                        message=(
                            f"архівовано {len(orphans)} робіт зі зниклих вкладок: "
                            + ", ".join(gone_tabs)
                        ),
                    )
                )

        if trigger == "manual" or summary.created or summary.updated or summary.deleted:
            session.add(
                SyncLog(
                    direction="sheet_to_db",
                    status="ok",
                    message=(
                        f"trigger {trigger}; tabs {summary.tabs_processed}; "
                        f"rows {summary.rows_seen}; created {summary.created}; "
                        f"updated {summary.updated}; unchanged {summary.unchanged}; "
                        f"deleted {summary.deleted}"
                    ),
                )
            )
        session.commit()
        return summary
    finally:
        _sync_lock.release()


def sync_sheets_background(session: Session) -> SheetSyncSummary:
    """Background-job entry point sharing the same locking and audit path."""
    return sync_google_sheets(session, trigger="background")


def sync_hot_tab(
    session: Session,
    *,
    today: date | None = None,
    extra_days: set[date] | None = None,
) -> SheetSyncSummary | None:
    """Fast lane: re-read only the operationally "hot" tabs — today's and
    yesterday's, plus ``extra_days`` (the days operators are viewing right now,
    tracked by the queue's poll — so "the open tab in the CRM" is always fast).
    Yesterday stays hot because the morning handout works out of yesterday's
    tab, and the user's "current day" is whichever tab the floor is actually
    in, not the calendar date.

    The full sync (worksheets listing + 3 tabs) costs tens of seconds through
    the lab proxy, but with the per-thread spreadsheet/worksheet cache warm
    (app/sheets.py) a single tab's `get_all_values` is ~3s — cheap enough to
    poll every ~15s so technician edits reach the CRM almost live (and a
    mistyped comment that gets corrected self-heals within one tick). Runs on
    the same background worker thread as the full sync, sharing its warm cache.

    Never raises on the busy lock — if a full/manual sync is in flight it
    already covers these tabs, so this tick just returns None ("skipped"). No
    SyncLog rows: this runs four times a minute and would flood the audit
    table; the full sync keeps owning audit logging. Each tab commits on its
    own (a failure on the second tab never rolls back the first). Returns a
    summary or None when skipped / no hot tab exists yet."""
    if not _sync_lock.acquire(blocking=False):
        return None
    try:
        _configuration(session)
        spreadsheet = open_spreadsheet(db=session)
        base_day = today or date.today()
        hot_days = [base_day, base_day - timedelta(days=1)]
        for extra in sorted(extra_days or ()):
            if extra not in hot_days:
                hot_days.append(extra)
        summary = SheetSyncSummary()
        for day in hot_days:
            tab_title = tab_name_for(day)
            worksheet = get_worksheet_by_name(spreadsheet, tab_title)
            if worksheet is None:
                continue  # tab not created yet (early morning) — skip
            raw = call_with_retry(worksheet.get_all_values)
            rows = parse_rows(raw)
            # Colours are cheap now (the CF-bloat cleanup took the metadata
            # fetch from ~7s to ~0.3s), so the hot lane reads them too: blue
            # clears flip to "видано" and grey SLM rows are filtered within
            # one ~15s tick instead of waiting for the next full sync.
            row_fills = fetch_row_fills(worksheet)
            result = sync_tab(
                session, tab_title, rows, row_fills=row_fills, raw_row_count=len(raw),
            )
            session.commit()
            summary.tabs_processed += 1
            summary.tab_names.append(tab_title)
            summary.rows_seen += len(rows)
            summary.created += result.created
            summary.updated += result.updated
            summary.unchanged += result.unchanged
            summary.deleted += result.deleted
        if summary.tabs_processed == 0:
            return None
        return summary
    except Exception as exc:
        session.rollback()
        raise _safe_failure(exc) from exc
    finally:
        _sync_lock.release()
