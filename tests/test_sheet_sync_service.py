from datetime import date, timedelta
from unittest.mock import Mock

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
    sync_sheets_background,
)


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


def test_remote_failure_rolls_back_orders_and_logs_sanitized_error(monkeypatch):
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

        assert session.query(Order).count() == 0
        log = session.scalar(select(SyncLog))
        assert log.status == "error"
        assert log.sheet_tab == broken.title
        assert secret not in log.message


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
