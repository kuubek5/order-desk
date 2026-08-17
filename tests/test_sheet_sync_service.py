from datetime import date, datetime, timedelta
from unittest.mock import Mock

import gspread
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Order, SyncLog
from app.sheet_sync_service import (
    SheetSyncBusyError,
    SheetSyncConfigurationError,
    SheetSyncError,
    _sync_lock,
    sync_google_sheets,
    sync_hot_tab,
    sync_sheets_background,
)
from app.sheets import reset_sheets_cache


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def worksheet(day: date, work_order: str = "24122") -> Mock:
    ws = Mock()
    ws.title = day.strftime("%d.%m.%y")
    row = ["1", work_order, "2", "моно A2", "анатомія", "x"]
    ws.get_all_values.return_value = ([[]] * 6) + [row]
    return ws


def configured(monkeypatch):
    monkeypatch.setattr(
        "app.sheet_sync_service.get_google_sheet_id", lambda session: "sheet-id"
    )
    monkeypatch.setattr(
        "app.sheet_sync_service.get_google_service_account_json",
        lambda session: '{"type": "service_account"}',
    )


def test_full_history_imports_all_dated_tabs_including_old(monkeypatch):
    """full_history=True must pull EVERY dated tab, even one far outside the
    default 30-day window (the «Імпортувати всю історію» action), while still
    skipping non-dated tabs."""
    configured(monkeypatch)
    today = date.today()
    current = worksheet(today, "200")
    too_old = worksheet(today - timedelta(days=200), "400")
    invalid = Mock(title="Підсумок")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [invalid, too_old, current]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        result = sync_google_sheets(session, full_history=True)

        assert result.tabs_processed == 2
        assert set(result.tab_names) == {current.title, too_old.title}
        assert session.query(Order).count() == 2


def test_first_sync_imports_recent_date_tabs_and_returns_summary(monkeypatch):
    configured(monkeypatch)
    today = date.today()
    recent = worksheet(today - timedelta(days=5), "100")
    current = worksheet(today, "200")
    future = worksheet(today + timedelta(days=1), "300")
    too_old = worksheet(today - timedelta(days=31), "400")
    invalid = Mock(title="Підсумок")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [invalid, future, too_old, current, recent]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        result = sync_google_sheets(session)

        assert result.tabs_processed == 3
        assert result.created == 3
        assert result.updated == 0
        assert result.unchanged == 0
        assert result.rows_seen == 3
        assert result.tab_names == [recent.title, current.title, future.title]
        assert session.query(Order).count() == 3
        log = session.scalar(select(SyncLog))
        assert log.status == "ok"
        assert log.direction == "sheet_to_db"
        assert "tabs 3" in log.message


def test_later_sync_uses_yesterday_today_tomorrow_only(monkeypatch):
    configured(monkeypatch)
    today = date.today()
    spreadsheet = Mock()
    old = worksheet(today - timedelta(days=2), "old")
    yesterday = worksheet(today - timedelta(days=1), "yesterday")
    spreadsheet.worksheets.return_value = [old, yesterday]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        session.add(Order(source="lab", sheet_tab="old", row_number=1, status="нове"))
        session.commit()
        result = sync_google_sheets(session)

        assert result.tabs_processed == 1
        assert result.tab_names == [yesterday.title]
        old.get_all_values.assert_not_called()


def test_orders_from_deleted_tabs_are_archived(monkeypatch):
    """A whole dated tab deleted from the sheet ARCHIVES its orders (lab AND
    sheet_client) on the next full sync — kept in the DB for the Archive, out
    of the working queue, never hard-deleted (the lab prunes old tabs for
    space). Email orders and orders with a non-dated sheet_tab are never
    touched. Re-running is idempotent (already-archived rows aren't re-stamped)."""
    configured(monkeypatch)
    today = date.today()
    current = worksheet(today, "200")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [current]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    gone_tab = (today - timedelta(days=3)).strftime("%d.%m.%y")
    with make_session() as session:
        session.add(Order(source="lab", sheet_tab=gone_tab, row_number=1,
                          work_order_no="111", status="нове"))
        session.add(Order(source="sheet_client", sheet_tab=gone_tab, row_number=60,
                          client_name="Vision", status="нове"))
        # must survive active: email order stamped with the same business date
        session.add(Order(source="email", sheet_tab=gone_tab, status="нове"))
        # must survive active: non-dated sheet_tab was never a real sheet tab
        session.add(Order(source="lab", sheet_tab="Підсумок", row_number=2, status="нове"))
        session.commit()

        result = sync_google_sheets(session)

        archived = session.scalars(
            select(Order.source).where(Order.archived_at.isnot(None))
        ).all()
        assert sorted(archived) == ["lab", "sheet_client"]
        active = session.scalars(
            select(Order.source).where(
                Order.archived_at.is_(None), Order.sheet_tab != current.title
            )
        ).all()
        assert sorted(active) == ["email", "lab"]
        assert result.deleted == 2
        logs = session.scalars(select(SyncLog)).all()
        assert any("зниклих вкладок" in log.message and gone_tab in log.message for log in logs)

        # Idempotent: a second full sync must not re-archive or re-log them.
        result2 = sync_google_sheets(session)
        assert result2.deleted == 0


def test_include_tabs_forces_an_out_of_window_tab(monkeypatch):
    # A manual sync launched from an older day (?date=...) force-reads that tab
    # even though it's well outside the yesterday/today/tomorrow window, so a
    # deletion there can be reconciled.
    configured(monkeypatch)
    today = date.today()
    old = worksheet(today - timedelta(days=10), "old")
    yesterday = worksheet(today - timedelta(days=1), "y")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [old, yesterday]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        # Existing lab data → steady-state three-day window (old normally skipped).
        session.add(Order(source="lab", sheet_tab="seed", row_number=1, status="нове"))
        session.commit()
        result = sync_google_sheets(session, include_tabs={old.title})

        assert old.title in result.tab_names
        old.get_all_values.assert_called_once()


def test_missing_credentials_is_logged_and_raised_without_opening_sheet(monkeypatch):
    monkeypatch.setattr(
        "app.sheet_sync_service.get_google_sheet_id", lambda session: "sheet-id"
    )
    monkeypatch.setattr(
        "app.sheet_sync_service.get_google_service_account_json", lambda session: None
    )
    open_sheet = Mock()
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", open_sheet)

    with make_session() as session:
        with pytest.raises(SheetSyncConfigurationError, match="JSON сервісного"):
            sync_google_sheets(session)

        open_sheet.assert_not_called()
        log = session.scalar(select(SyncLog))
        assert log.status == "error"
        assert "JSON сервісного" in log.message


def test_failing_tab_rolls_back_but_earlier_tabs_persist_and_error_is_sanitized(monkeypatch):
    # good (today) is imported and committed first; broken (tomorrow) fails.
    # Per-tab commit means good survives while only the failing tab is rolled
    # back — the whole run is no longer discarded. The raw exception text (a
    # private key here) must never reach the persisted error log.
    configured(monkeypatch)
    secret = "SUPER-SECRET-PRIVATE-KEY"
    good = worksheet(date.today(), "100")
    broken = worksheet(date.today() + timedelta(days=1), "200")
    broken.get_all_values.side_effect = RuntimeError(secret)
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [good, broken]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        with pytest.raises(SheetSyncError, match="Не вдалося синхронізувати"):
            sync_google_sheets(session)

        # good's order stayed committed; broken's did not create anything.
        orders = session.query(Order).all()
        assert len(orders) == 1
        assert orders[0].sheet_tab == good.title
        log = session.scalar(select(SyncLog).where(SyncLog.status == "error"))
        assert log.status == "error"
        assert log.sheet_tab == broken.title
        assert secret not in log.message


def test_setup_failure_imports_nothing(monkeypatch):
    # A failure before the per-tab loop (here: opening the spreadsheet) leaves
    # the DB empty and records the sanitized error, since there is no partial
    # progress to preserve.
    configured(monkeypatch)

    def boom(db):
        raise RuntimeError("cannot open")

    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", boom)

    with make_session() as session:
        with pytest.raises(SheetSyncError, match="Не вдалося синхронізувати"):
            sync_google_sheets(session)

        assert session.query(Order).count() == 0
        log = session.scalar(select(SyncLog))
        assert log.status == "error"
        assert log.sheet_tab is None


def test_invalid_date_tabs_are_ignored(monkeypatch):
    configured(monkeypatch)
    invalid_date = Mock(title="31.02.26")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [invalid_date]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        result = sync_google_sheets(session)

        assert result.tabs_processed == 0
        invalid_date.get_all_values.assert_not_called()


def test_busy_lock_rejects_concurrent_sync(monkeypatch):
    configured(monkeypatch)
    # Shrink the manual grace wait so the test doesn't sit through the real 10s.
    monkeypatch.setattr("app.sheet_sync_service._MANUAL_LOCK_WAIT_SECONDS", 0.05)
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = []
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    _sync_lock.acquire()
    try:
        with make_session() as session:
            with pytest.raises(SheetSyncBusyError):
                sync_google_sheets(session)
    finally:
        _sync_lock.release()


def test_manual_sync_waits_out_a_short_hot_tick(monkeypatch):
    # A manual click that lands during a ~3s hot-tab tick must WAIT and then
    # run, not bounce with "вже виконується" (the hot lane holds the same lock
    # every 15s, so instant failure would hit real users constantly).
    import threading

    configured(monkeypatch)
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = []
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    _sync_lock.acquire()
    threading.Timer(0.2, _sync_lock.release).start()
    with make_session() as session:
        result = sync_google_sheets(session)  # manual trigger by default
    assert result.tabs_processed == 0


def test_background_trigger_skips_log_when_nothing_changed(monkeypatch):
    configured(monkeypatch)
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = []
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    with make_session() as session:
        result = sync_sheets_background(session)

        assert result.tabs_processed == 0
        assert session.scalar(select(SyncLog)) is None


def test_background_trigger_logs_when_rows_created(monkeypatch):
    configured(monkeypatch)
    fresh = worksheet(date.today(), "500")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [fresh]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    with make_session() as session:
        result = sync_sheets_background(session)

        assert result.created == 1
        log = session.scalar(select(SyncLog))
        assert log is not None
        assert "trigger background" in log.message


def test_sync_hot_tab_reads_today_and_yesterday_by_name(monkeypatch):
    # The fast lane must fetch the hot tabs (today + yesterday, the
    # morning-handout tab) by name — never the expensive worksheets() listing
    # the full sync pays for.
    configured(monkeypatch)
    reset_sheets_cache()
    today = date.today()
    today_ws = worksheet(today, "700")
    yesterday_ws = worksheet(today - timedelta(days=1), "701")
    by_name = {today_ws.title: today_ws, yesterday_ws.title: yesterday_ws}
    spreadsheet = Mock()
    spreadsheet.worksheet.side_effect = lambda name: by_name[name]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        summary = sync_hot_tab(session)

        assert summary is not None
        assert summary.tabs_processed == 2
        assert summary.tab_names == [today_ws.title, yesterday_ws.title]
        assert summary.created == 2
        spreadsheet.worksheets.assert_not_called()
        assert spreadsheet.worksheet.call_count == 2
        # No audit rows from the 15s cadence — the full sync owns SyncLog.
        assert session.scalar(select(SyncLog)) is None
    reset_sheets_cache()


def test_sync_hot_tab_skips_quietly_when_lock_busy(monkeypatch):
    configured(monkeypatch)
    reset_sheets_cache()
    open_sheet = Mock()
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", open_sheet)

    _sync_lock.acquire()
    try:
        with make_session() as session:
            assert sync_hot_tab(session) is None
    finally:
        _sync_lock.release()
    open_sheet.assert_not_called()


def test_sync_hot_tab_returns_none_when_todays_tab_missing(monkeypatch):
    # Early morning: technicians haven't created today's tab yet.
    configured(monkeypatch)
    reset_sheets_cache()
    spreadsheet = Mock()
    spreadsheet.worksheet.side_effect = gspread.WorksheetNotFound("no tab")
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        assert sync_hot_tab(session) is None
    reset_sheets_cache()


def test_sync_hot_tab_picks_up_edit_and_deletion(monkeypatch):
    # A technician fixes a comment and removes a row in today's tab — one hot
    # tick converges the CRM (the "typo fixed within ~15s" contract).
    configured(monkeypatch)
    reset_sheets_cache()
    today = date.today()
    two_rows = Mock()
    two_rows.title = today.strftime("%d.%m.%y")
    two_rows.get_all_values.return_value = ([[]] * 6) + [
        ["1", "800", "2", "моно A2", "анатомія", "x", "", "", "", "", "помилковий текст"],
        ["2", "801", "1", "пмма A3", "коронка", "x"],
    ]
    spreadsheet = Mock()

    def only_today(name):
        if name == two_rows.title:
            return two_rows
        raise gspread.WorksheetNotFound(name)

    spreadsheet.worksheet.side_effect = only_today
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        first = sync_hot_tab(session, today=today)
        assert first.created == 2

        # Age past sync_tab's deletion grace window (fresh orders are shielded
        # from reconciliation — the manual-add race guard in app/sync.py).
        aged = datetime.utcnow() - timedelta(minutes=10)
        for order in session.scalars(select(Order)):
            order.created_at = aged
        session.commit()

        # Fix the comment on row 1, delete row 2 entirely.
        two_rows.get_all_values.return_value = ([[]] * 6) + [
            ["1", "800", "2", "моно A2", "анатомія", "x", "", "", "", "", "виправлений текст"],
        ]
        reset_sheets_cache()  # fresh worksheet lookup for the second tick
        second = sync_hot_tab(session, today=today)

        assert second.updated == 1
        assert second.deleted == 1
        survivor = session.scalar(select(Order).where(Order.work_order_no == "800"))
        assert survivor.cam_comment == "виправлений текст"
        # Deleted row is archived (kept), not removed from the DB.
        gone = session.scalar(select(Order).where(Order.work_order_no == "801"))
        assert gone is not None and gone.archived_at is not None
    reset_sheets_cache()


def test_sync_hot_tab_includes_extra_viewed_days(monkeypatch):
    # An operator viewing an older day (queue day-strip) makes that tab hot
    # too — "the open tab in the CRM" must be among the fast-synced ones.
    configured(monkeypatch)
    reset_sheets_cache()
    today = date.today()
    old_day = today - timedelta(days=9)
    old_ws = worksheet(old_day, "900")
    by_name = {old_ws.title: old_ws}
    spreadsheet = Mock()

    def by_title(name):
        if name in by_name:
            return by_name[name]
        raise gspread.WorksheetNotFound(name)

    spreadsheet.worksheet.side_effect = by_title
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        summary = sync_hot_tab(session, extra_days={old_day})

        assert summary is not None
        assert old_ws.title in summary.tab_names
        assert summary.created == 1
    reset_sheets_cache()


def test_background_failure_is_not_persisted(monkeypatch):
    configured(monkeypatch)
    broken = worksheet(date.today(), "600")
    broken.get_all_values.side_effect = RuntimeError("boom")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [broken]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    with make_session() as session:
        with pytest.raises(SheetSyncError):
            sync_sheets_background(session)

        assert session.scalar(select(SyncLog)) is None
