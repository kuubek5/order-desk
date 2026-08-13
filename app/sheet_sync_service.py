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
from app.settings_store import get_google_service_account_json, get_google_sheet_id
from app.sheets import call_with_retry, open_spreadsheet
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


@dataclass
class SheetSyncSummary:
    tabs_processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    rows_seen: int = 0
    tab_names: list[str] = field(default_factory=list)


def _parse_tab_date(title: str) -> date | None:
    if not _DATE_TAB_RE.fullmatch(title):
        return None
    try:
        return datetime.strptime(title, "%d.%m.%y").date()
    except ValueError:
        return None


def _worksheets_to_sync(session: Session, spreadsheet, today: date) -> list:
    """Choose a bounded initial history, then the operational three-day window."""
    has_sheet_orders = session.scalar(
        select(func.count(Order.id)).where(Order.source == "lab")
    ) > 0
    first_day = today - timedelta(days=1 if has_sheet_orders else _INITIAL_LOOKBACK_DAYS)
    last_day = today + timedelta(days=1)

    dated = []
    for worksheet in call_with_retry(spreadsheet.worksheets):
        tab_date = _parse_tab_date(worksheet.title)
        if tab_date is not None and first_day <= tab_date <= last_day:
            dated.append((tab_date, worksheet.title, worksheet))
    dated.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in dated]


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


def sync_google_sheets(session: Session, *, trigger: str = "manual") -> SheetSyncSummary:
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
    if not _sync_lock.acquire(blocking=False):
        raise SheetSyncBusyError(
            "Синхронізація Google Таблиці вже виконується. Спробуйте трохи пізніше."
        )

    summary = SheetSyncSummary()
    try:
        try:
            _configuration(session)
            spreadsheet = open_spreadsheet(db=session)  # retries internally
            worksheets = _worksheets_to_sync(session, spreadsheet, date.today())
        except Exception as exc:
            # Setup failure: nothing has been imported, so there is no partial
            # progress to preserve — surface it and record it like before.
            safe_error = _safe_failure(exc)
            _record_failure(session, None, safe_error, persist=trigger == "manual")
            raise safe_error from exc

        for worksheet in worksheets:
            current_tab = worksheet.title
            try:
                rows = parse_rows(call_with_retry(worksheet.get_all_values))
                result = sync_tab(session, current_tab, rows)
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

        if trigger == "manual" or summary.created or summary.updated:
            session.add(
                SyncLog(
                    direction="sheet_to_db",
                    status="ok",
                    message=(
                        f"trigger {trigger}; tabs {summary.tabs_processed}; "
                        f"rows {summary.rows_seen}; created {summary.created}; "
                        f"updated {summary.updated}; unchanged {summary.unchanged}"
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
