from datetime import date, datetime, timedelta
from app.sync import SyncResult
from unittest.mock import Mock

import gspread
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.business_day import business_today
from app.db import Base
from app.models import Order, SyncLog
from app.sheet_sync_service import (
    _ABSENT_LISTINGS_BEFORE_ARCHIVE,
    SheetSyncBusyError,
    absent_tab_streaks,
    mass_vanish_pending,
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
    today = business_today()
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
    today = business_today()
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
    today = business_today()
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


def test_manual_sync_archives_a_just_created_row_deleted_from_the_sheet(monkeypatch):
    """Rома's report: add a наряд in the sheet, it appears in the CRM, delete it
    in the sheet — but it stays, and manual sync doesn't help. Cause: the just-
    imported order was inside the 120s deletion grace, and every manual sync in
    that window skipped it. A manual sync now reconciles immediately."""
    configured(monkeypatch)
    today = business_today()
    empty = worksheet(today)
    empty.get_all_values.return_value = [[]] * 6  # row cleared, headers remain
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [empty]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )
    monkeypatch.setattr(
        "app.sheet_sync_service.fetch_row_fills", lambda ws: {}
    )

    with make_session() as session:
        # Imported seconds ago (inside the grace), row 7 = data-row 1.
        session.add(Order(
            source="lab", sheet_tab=today.strftime("%d.%m.%y"), row_number=1,
            work_order_no="28700", status="нове", created_at=datetime.utcnow(),
        ))
        session.commit()

        # Background run keeps the grace — the fresh order survives.
        sync_google_sheets(session, trigger="background")
        assert session.scalar(select(Order)).archived_at is None

        # Manual run reconciles now.
        sync_google_sheets(session, trigger="manual")
        assert session.scalar(select(Order)).archived_at is not None


def test_orders_from_deleted_tabs_are_archived(monkeypatch):
    """A whole dated tab deleted from the sheet ARCHIVES its orders (lab AND
    sheet_client) on the next full sync — kept in the DB for the Archive, out
    of the working queue, never hard-deleted (the lab prunes old tabs for
    space). Email orders and orders with a non-dated sheet_tab are never
    touched. Re-running is idempotent (already-archived rows aren't re-stamped)."""
    configured(monkeypatch)
    today = business_today()
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

        # Разовий листинг без вкладки — ще не видалення (проксі віддає кешовані
        # відповіді): перші два читання лише рахують, у журналі — «skipped».
        for _ in range(2):
            early = sync_google_sheets(session)
            assert early.deleted == 0
            assert session.scalars(
                select(Order).where(Order.archived_at.isnot(None))
            ).all() == []
        early_logs = session.scalars(select(SyncLog)).all()
        assert sum(
            1 for log in early_logs
            if log.status == "skipped" and "зникла з листингу" in (log.message or "")
        ) == 1, "слід про зниклу вкладку пишеться ОДИН раз, не на кожен тік"

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
    today = business_today()
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
    good = worksheet(business_today(), "100")
    broken = worksheet(business_today() + timedelta(days=1), "200")
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
    fresh = worksheet(business_today(), "500")
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
    today = business_today()
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
    today = business_today()
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
    today = business_today()
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
    broken = worksheet(business_today(), "600")
    broken.get_all_values.side_effect = RuntimeError("boom")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [broken]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    with make_session() as session:
        with pytest.raises(SheetSyncError):
            sync_sheets_background(session)

        assert session.scalar(select(SyncLog)) is None


def test_catches_up_missed_days_after_being_offline(monkeypatch):
    """Головний сценарій прохання власника (31.08.26): CRM вимкнули на кілька
    днів, у таблиці за цей час з'явились нові дні — після ввімкнення вони
    мусять підтягнутись, а не лишитись поза чергою.

    Звичайне вікно — вчора±сьогодні±завтра. Тут останній успішний синк був
    4 дні тому, тому вікно мусить розсунутись назад до нього і забрати всі
    пропущені дні.
    """
    from app.sheet_sync_service import _mark_full_sync

    configured(monkeypatch)
    today = business_today()
    spreadsheet = Mock()
    # У таблиці — 4 дні, що накопичились за простій, плюс сьогодні.
    days = [worksheet(today - timedelta(days=n), f"d{n}") for n in (4, 3, 2, 1, 0)]
    spreadsheet.worksheets.return_value = days
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        # Є лаб-роботи (тобто це НЕ перший синк) і штамп «синхронізовано 4 дні
        # тому» — саме те, що лишається після кількаденного простою.
        session.add(Order(source="lab", sheet_tab="seed", row_number=99, status="нове"))
        session.commit()
        _mark_full_sync(session, today - timedelta(days=4))
        session.commit()

        result = sync_google_sheets(session)

        # Усі пропущені дні (4,3,2 тому) + вчора + сьогодні — усі в роботі.
        assert result.tabs_processed == 5, result.tab_names
        titles = {d.title for d in days}
        assert set(result.tab_names) == titles


def test_normal_run_still_reads_only_three_days_when_sync_is_fresh(monkeypatch):
    """Догоняючий механізм не має розширювати вікно, коли простою НЕ було:
    свіжий штамп (учора) лишає звичайне вузьке вікно, інакше кожен тік читав
    би зайві старі вкладки через повільний проксі."""
    from app.sheet_sync_service import _mark_full_sync

    configured(monkeypatch)
    today = business_today()
    spreadsheet = Mock()
    old = worksheet(today - timedelta(days=3), "old")
    yesterday = worksheet(today - timedelta(days=1), "yesterday")
    spreadsheet.worksheets.return_value = [old, yesterday]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        session.add(Order(source="lab", sheet_tab="seed", row_number=1, status="нове"))
        session.commit()
        _mark_full_sync(session, today - timedelta(days=1))  # синхронізовано вчора
        session.commit()

        result = sync_google_sheets(session)

        assert result.tab_names == [yesterday.title]
        old.get_all_values.assert_not_called()


def test_ancient_stamp_is_clamped_to_initial_window(monkeypatch):
    """Дуже старий штамп (місяці простою) не тягне пів року вкладок: вікно
    обмежене тим самим initial-lookback, що й перший синк."""
    from app.sheet_sync_service import _INITIAL_LOOKBACK_DAYS, _mark_full_sync

    configured(monkeypatch)
    today = business_today()
    spreadsheet = Mock()
    inside = worksheet(today - timedelta(days=_INITIAL_LOOKBACK_DAYS - 2), "inside")
    ancient = worksheet(today - timedelta(days=_INITIAL_LOOKBACK_DAYS + 40), "ancient")
    spreadsheet.worksheets.return_value = [ancient, inside]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        session.add(Order(source="lab", sheet_tab="seed", row_number=1, status="нове"))
        session.commit()
        _mark_full_sync(session, today - timedelta(days=_INITIAL_LOOKBACK_DAYS + 40))
        session.commit()

        result = sync_google_sheets(session)

        assert inside.title in result.tab_names
        assert ancient.title not in result.tab_names
        ancient.get_all_values.assert_not_called()


def test_successful_sync_records_todays_date_as_last_full(monkeypatch):
    """Після успішного синку штамп = сьогодні: наступний догоняючий розрахунок
    відштовхується від правильного дня."""
    from app.sheet_sync_service import _last_full_sync_date

    configured(monkeypatch)
    today = business_today()
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [worksheet(today, "200")]
    monkeypatch.setattr(
        "app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet
    )

    with make_session() as session:
        sync_google_sheets(session)
        assert _last_full_sync_date(session) == today


def test_mass_vanish_pending_records_and_clears():
    """Модульний стан банера: _record_mass_vanish виставляє лічильник для
    вкладки, а нульове значення прибирає її (вкладка синхронізувалась чисто /
    підтверджено). Живить mass_vanish_pending() → банер на черзі."""
    from app.sheet_sync_service import _record_mass_vanish, mass_vanish_pending

    _record_mass_vanish("01.01.30", 12)
    assert mass_vanish_pending().get("01.01.30") == 12
    # returned dict is a copy — мутація ззовні не псує внутрішній стан
    mass_vanish_pending()["01.01.30"] = 999
    assert mass_vanish_pending().get("01.01.30") == 12
    _record_mass_vanish("01.01.30", 0)
    assert "01.01.30" not in mass_vanish_pending()


# --- Запобіжник на «вкладка зникла цілком» (аудит 05.09.26, синк H-3/H-4) ----


def _clear_vanish_state():
    from app.sheet_sync_service import _mass_vanish_pending, _mass_vanish_lock

    with _mass_vanish_lock:
        _mass_vanish_pending.clear()


@pytest.fixture(autouse=True)
def _reset_mass_vanish():
    _clear_vanish_state()
    yield
    _clear_vanish_state()


def _many_orphans(session, gone_tab, count=8):
    for i in range(count):
        session.add(Order(source="lab", sheet_tab=gone_tab, row_number=i + 1,
                          work_order_no=f"9{i:03d}", status="нове"))
    session.commit()


def test_bulk_vanished_tab_is_held_not_archived(monkeypatch):
    """Неповний листинг вкладок виглядає точно як «день видалили». Порядковий
    синк має поріг >5 І >25%, а ця гілка не мала жодного — і цілі робочі дні
    йшли в Архів, звідки самі не повертаються (фонове вікно їх не читає)."""
    configured(monkeypatch)
    today = business_today()
    current = worksheet(today, "200")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [current]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    gone_tab = (today - timedelta(days=3)).strftime("%d.%m.%y")
    with make_session() as session:
        _many_orphans(session, gone_tab)

        result = sync_google_sheets(session)

        assert result.deleted == 0
        assert session.scalars(
            select(Order).where(Order.archived_at.isnot(None))
        ).all() == []
        logs = [log.message for log in session.scalars(select(SyncLog)).all()]
        assert any("притримано архівацію" in message for message in logs)
        # Банер бачить саме цю вкладку — і «Звірити видалення» цілиться в неї.
        assert mass_vanish_pending().get(gone_tab) == 8


def test_confirmed_tab_is_archived_while_its_neighbour_stays_protected(monkeypatch):
    """«Звірити видалення» знімає поріг ЛИШЕ для підтверджених вкладок."""
    configured(monkeypatch)
    today = business_today()
    current = worksheet(today, "200")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [current]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    confirmed_tab = (today - timedelta(days=3)).strftime("%d.%m.%y")
    other_tab = (today - timedelta(days=4)).strftime("%d.%m.%y")
    with make_session() as session:
        _many_orphans(session, confirmed_tab, count=8)
        _many_orphans(session, other_tab, count=8)

        result = sync_google_sheets(
            session, trigger="manual", force_reconcile_tabs={confirmed_tab}
        )

        archived_tabs = set(session.scalars(
            select(Order.sheet_tab).where(Order.archived_at.isnot(None))
        ).all())
        assert archived_tabs == {confirmed_tab}
        assert result.deleted == 8


def test_listing_without_today_or_yesterday_archives_nothing(monkeypatch):
    """Листинг без свіжої вкладки — це не «видалили дні», а недостовірна
    відповідь (кеш/обрив проксі). Архівувати за нею не можна."""
    configured(monkeypatch)
    today = business_today()
    stale = worksheet(today - timedelta(days=10), "old")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [stale]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    gone_tab = (today - timedelta(days=3)).strftime("%d.%m.%y")
    with make_session() as session:
        session.add(Order(source="lab", sheet_tab="seed", row_number=1, status="нове"))
        session.add(Order(source="lab", sheet_tab=gone_tab, row_number=2,
                          work_order_no="111", status="нове"))
        session.commit()

        result = sync_google_sheets(session)

        assert result.deleted == 0
        assert session.scalars(
            select(Order).where(Order.archived_at.isnot(None))
        ).all() == []


def test_force_reconcile_flag_reaches_only_the_confirmed_tab(monkeypatch):
    """Прапорець «я справді видалив пачку» більше не булевий на весь прогін:
    сусідня вкладка, прочитана обрізано, лишається під захистом (синк H-4)."""
    configured(monkeypatch)
    today = business_today()
    current = worksheet(today, "200")
    yesterday = worksheet(today - timedelta(days=1), "201")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [yesterday, current]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    seen: dict[str, bool] = {}

    def fake_sync_tab(session, tab, rows, **kwargs):
        seen[tab] = kwargs["force_reconcile"]
        # Справжній SyncResult, а не саморобний namespace: підсумок синку
        # читає з нього і лічильники звірки, і будь-що, що додадуть пізніше.
        return SyncResult()

    monkeypatch.setattr("app.sheet_sync_service.sync_tab", fake_sync_tab)

    with make_session() as session:
        sync_google_sheets(
            session, trigger="manual", force_reconcile_tabs={current.title}
        )

    assert seen == {current.title: True, yesterday.title: False}


# --- S2.7: довіра до листингу й позначка «день синхронізовано» --------------


def test_listing_without_today_is_trusted_when_it_reaches_what_we_already_saw():
    """CRM можна вимикати на дні: у понеділок найновіша вкладка законно з
    пʼятниці. Вимога «мусить бути сьогоднішня» робила такий листинг
    недостовірним НАЗАВЖДИ, тобто вкладки, видалені за час простою, ніколи не
    прибирались із черги."""
    from datetime import date as _date

    from app.sheet_sync_service import _listing_is_trustworthy

    today = _date(2026, 9, 7)          # понеділок
    listing = {"04.09.26", "03.09.26"}  # найновіша — пʼятниця

    assert _listing_is_trustworthy(listing, today, newest_known=_date(2026, 9, 4))


def test_listing_older_than_what_we_imported_is_not_trusted():
    """А от листинг, що не дотягує навіть до вже імпортованої вкладки, — це
    відповідь із минулого (кеш проксі), і архівувати за ним не можна."""
    from datetime import date as _date

    from app.sheet_sync_service import _listing_is_trustworthy

    today = _date(2026, 9, 7)
    listing = {"01.09.26"}

    assert not _listing_is_trustworthy(listing, today, newest_known=_date(2026, 9, 4))


def test_listing_with_today_is_trusted_regardless():
    from datetime import date as _date

    from app.sheet_sync_service import _listing_is_trustworthy

    today = _date(2026, 9, 7)

    assert _listing_is_trustworthy({"07.09.26"}, today, newest_known=None)
    assert not _listing_is_trustworthy(set(), today, newest_known=None)


def test_empty_run_does_not_count_as_a_synced_day(monkeypatch):
    """Порожній прогін (проксі віддав нічого) не має закривати догін простою:
    інакше пропущені дні більше не перечитались би ніколи."""
    from app.sheet_sync_service import _last_full_sync_date

    configured(monkeypatch)
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = []   # листинг порожній
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    with make_session() as session:
        summary = sync_google_sheets(session, trigger="background")
        assert summary.tabs_processed == 0
        assert _last_full_sync_date(session) is None


# --- 08.09.26: один листинг без сьогоднішньої вкладки забрав день в Архів ----


def test_listing_without_todays_tab_but_with_future_tabs_holds_the_day(monkeypatch):
    """Бойовий випадок 08.09.26. Лабораторія створює вкладки наперед
    (09.09–14.09), тож листинг БЕЗ сьогоднішньої вкладки все одно «дотягує до
    вчора» і проходить перевірку довіри. Далі ~100 робіт дня — це менше за 25 %
    від 30-денної черги, і поріг масового зникнення їх не ловив: день ішов в
    Архів за один тік, а черга показувала «Сьогодні 0».

    Вкладку робочого вікна ніхто не видаляє свідомо: більше за
    _VANISHED_TAB_MIN_ORDERS робіт у ній — тримати й питати, не архівувати."""
    from app.sheet_sync_service import _VANISHED_TAB_MIN_ORDERS

    configured(monkeypatch)
    today = business_today()
    listing = [
        worksheet(today - timedelta(days=1), "201"),
        worksheet(today + timedelta(days=1), "202"),
        worksheet(today + timedelta(days=6), "203"),
    ]
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = listing
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    today_tab = today.strftime("%d.%m.%y")
    with make_session() as session:
        _many_orphans(session, today_tab, count=_VANISHED_TAB_MIN_ORDERS + 1)
        # Довга черга: день — лише ~10 % активних робіт, частка 25 % не ловить.
        for i in range(60):
            session.add(Order(source="lab", sheet_tab="seed", row_number=i + 1,
                              work_order_no=f"7{i:03d}", status="нове"))
        session.commit()

        for _ in range(_ABSENT_LISTINGS_BEFORE_ARCHIVE + 1):
            result = sync_google_sheets(session)
            assert result.deleted == 0

        assert session.scalars(
            select(Order).where(Order.archived_at.isnot(None))
        ).all() == []
        # Банер: саме сьогоднішня вкладка, і «Звірити видалення» цілиться в неї.
        assert mass_vanish_pending().get(today_tab) == _VANISHED_TAB_MIN_ORDERS + 1
        logs = [log.message or "" for log in session.scalars(select(SyncLog)).all()]
        assert any("притримано архівацію" in message for message in logs)


def test_tab_that_reappears_resets_the_absent_streak(monkeypatch):
    """Лічильник рахує читання ПОСПІЛЬ: вкладка, що зникла й повернулась,
    починає з нуля. Інакше три розрізнені збої проксі за день складались би
    в «видалення»."""
    configured(monkeypatch)
    today = business_today()
    current = worksheet(today, "200")
    gone_day = today - timedelta(days=3)
    old = worksheet(gone_day, "199")
    spreadsheet = Mock()
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    gone_tab = gone_day.strftime("%d.%m.%y")
    with make_session() as session:
        # Дрібна вкладка: нижче порога масового зникнення, тож єдиний захист —
        # лічильник читань.
        _many_orphans(session, gone_tab, count=2)

        spreadsheet.worksheets.return_value = [current]          # нема (1)
        assert sync_google_sheets(session).deleted == 0
        spreadsheet.worksheets.return_value = [old, current]     # є → скидання
        assert sync_google_sheets(session).deleted == 0
        assert absent_tab_streaks() == {}
        spreadsheet.worksheets.return_value = [current]          # нема (1)
        assert sync_google_sheets(session).deleted == 0
        spreadsheet.worksheets.return_value = [current]          # нема (2)
        assert sync_google_sheets(session).deleted == 0
        assert absent_tab_streaks() == {gone_tab: 2}
        spreadsheet.worksheets.return_value = [current]          # нема (3)
        assert sync_google_sheets(session).deleted == 2
        # Після архівації лічильник не висить.
        assert absent_tab_streaks() == {}
        archived_tabs = set(session.scalars(
            select(Order.sheet_tab).where(Order.archived_at.isnot(None))
        ).all())
        assert archived_tabs == {gone_tab}


def test_confirmed_tab_is_archived_without_waiting(monkeypatch):
    """«Звірити видалення» — свідоме рішення оператора: лічильник читань його
    не затримує."""
    configured(monkeypatch)
    today = business_today()
    current = worksheet(today, "200")
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [current]
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)

    gone_tab = (today - timedelta(days=3)).strftime("%d.%m.%y")
    with make_session() as session:
        _many_orphans(session, gone_tab, count=2)
        result = sync_google_sheets(
            session, trigger="manual", force_reconcile_tabs={gone_tab}
        )
        assert result.deleted == 2
