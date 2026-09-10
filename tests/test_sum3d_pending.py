"""Sum3D, що не дійшов у таблицю, не зникає мовчки (10.09.26).

Бойовий випадок: оператор у фільтрі «Можна брати» вписав Sum3D трьом
лабораторним роботам — одна «не записалась». Механізм: база зберігає ID
одразу, таблиця — після; запис у таблицю не підтвердився, попередження жило
один рендер (15-с полл його прибрав), а наступний синк побачив порожню L і
стер ID — таблиця головна для Sum3D.

Стережемо:
1. порожня L не стирає ID, що чекає на запис, — а без позначки стирає, як і
   досі (правило «повернути в чергу, очистивши L» живе);
2. позначку ставить і знімає запис у таблицю, а таблиця з ID її знімає;
3. фоновий повтор дописує, не частіше за інтервал і не під паузою;
4. попередження на рядку стоїть у звичайному рендері черги, не лише у
   відповіді на сам запис.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Order
from app.services import sheet_writeback as wb
from app.sync import sync_tab
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура
from tests.test_sync import make_row, make_session


def _pending_order(session, *, sum3d="16-27-26", letter="М") -> Order:
    """Робота «в роботі» за базою, а в таблиці L і M ще порожні."""
    sync_tab(session, "T", [make_row(sum3d_id="", calculated="", milled="")])
    session.commit()
    order = session.scalar(select(Order))
    order.sum3d_id = sum3d
    order.calculated_raw = letter
    order.sum3d_pending = sum3d
    session.commit()
    return order


class TestSyncKeepsPendingSum3D:
    def test_empty_sheet_cell_does_not_erase_a_pending_id(self):
        with make_session() as session:
            order = _pending_order(session)
            sync_tab(session, "T", [make_row(sum3d_id="", calculated="", milled="")])
            session.commit()
            session.refresh(order)
            assert order.sum3d_id == "16-27-26", "ID, що не дійшов у таблицю, стерто синком"
            assert order.calculated_raw == "М", "літеру «Прорахував» стерто разом"
            assert order.sum3d_pending == "16-27-26"

    def test_sheet_with_the_id_clears_the_flag(self):
        with make_session() as session:
            order = _pending_order(session)
            sync_tab(session, "T", [make_row(sum3d_id="16-27-26", calculated="М", milled="")])
            session.commit()
            session.refresh(order)
            assert order.sum3d_id == "16-27-26"
            assert order.sum3d_pending is None

    def test_someone_elses_id_in_the_sheet_still_wins(self):
        """Таблиця головна: хтось вписав у L інше — беремо його, позначку знято."""
        with make_session() as session:
            order = _pending_order(session)
            sync_tab(session, "T", [make_row(sum3d_id="09-00-00", calculated="", milled="")])
            session.commit()
            session.refresh(order)
            assert order.sum3d_id == "09-00-00"
            assert order.sum3d_pending is None

    def test_stale_flag_protects_nothing(self):
        """ID у базі змінився мимо запису — позначка про інше значення нічого не
        тримає, і порожня L стирає, як завжди."""
        with make_session() as session:
            order = _pending_order(session)
            order.sum3d_id = "11-11-11"
            session.commit()
            sync_tab(session, "T", [make_row(sum3d_id="", calculated="", milled="")])
            session.commit()
            session.refresh(order)
            assert order.sum3d_id is None
            assert order.sum3d_pending is None

    def test_without_the_flag_empty_sheet_still_hands_the_work_back(self):
        """Головне правило черги не зачеплено: очистили L — робота «можна брати»."""
        with make_session() as session:
            sync_tab(session, "T", [make_row(sum3d_id="12-01-45", calculated="", milled="")])
            session.commit()
            sync_tab(session, "T", [make_row(sum3d_id="", calculated="", milled="")])
            session.commit()
            assert session.scalar(select(Order)).sum3d_id is None


class TestWriteSetsAndClearsTheFlag:
    @pytest.fixture
    def sheet(self, monkeypatch):
        state = SimpleNamespace(written=True, calls=0)

        def write(ws, order, fields):
            state.calls += 1
            return state.written

        monkeypatch.setattr(wb, "open_spreadsheet", lambda db=None: object())
        monkeypatch.setattr(wb, "get_worksheet_by_name", lambda ss, tab: object())
        monkeypatch.setattr(wb, "write_order_fields", write)
        return state

    def _order(self, session) -> Order:
        order = Order(source="lab", sheet_tab="10.09.26", row_number=7, sum3d_id="16-27-26")
        session.add(order)
        session.commit()
        return order

    def test_unconfirmed_row_leaves_the_id_pending(self, sheet):
        sheet.written = False
        with make_session() as session:
            order = self._order(session)
            assert wb.write_sheet_fields(session, order, {"sum3d_id"})
            assert order.sum3d_pending == "16-27-26"

    def test_network_error_leaves_the_id_pending(self, monkeypatch, sheet):
        def boom(ws, order, fields):
            raise OSError("proxy reset")

        monkeypatch.setattr(wb, "write_order_fields", boom)
        with make_session() as session:
            order = self._order(session)
            assert wb.write_sheet_fields(session, order, {"sum3d_id"}) == "proxy reset"
            assert order.sum3d_pending == "16-27-26"

    def test_successful_write_clears_the_flag(self, sheet):
        with make_session() as session:
            order = self._order(session)
            order.sum3d_pending = "16-27-26"
            assert wb.write_sheet_fields(session, order, {"sum3d_id"}) is None
            assert order.sum3d_pending is None

    def test_a_failed_clear_is_not_held(self, sheet):
        """Невдале ОЧИЩЕННЯ не тримаємо: у таблиці старий ID, синк його поверне,
        і оператор побачить, що не очистилось."""
        sheet.written = False
        with make_session() as session:
            order = self._order(session)
            order.sum3d_id = None
            wb.write_sheet_fields(session, order, {"sum3d_id"})
            assert order.sum3d_pending is None

    def test_other_fields_do_not_touch_the_flag(self, sheet):
        sheet.written = False
        with make_session() as session:
            order = self._order(session)
            order.sum3d_pending = "16-27-26"
            wb.write_sheet_fields(session, order, {"cam_comment"})
            assert order.sum3d_pending == "16-27-26"


class TestBackgroundRetry:
    @pytest.fixture
    def submitted(self, monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr(wb, "submit_sheet_write", lambda fn, *args: calls.append((fn, args)))
        monkeypatch.setattr(wb.sync_control, "is_paused", lambda: False)
        monkeypatch.setattr("app.sheets.quota_is_tight", lambda now=None: False)
        monkeypatch.setattr(wb, "_pending_sum3d_attempts", {})
        return calls

    def _seed(self, session):
        session.add_all([
            Order(source="lab", sheet_tab="T", row_number=1, sum3d_id="16-27-26",
                  sum3d_pending="16-27-26", calculated_raw="М"),
            Order(source="lab", sheet_tab="T", row_number=2, sum3d_id="10-00-00"),
            # Застаріла позначка — повторювати нема чого.
            Order(source="lab", sheet_tab="T", row_number=3, sum3d_id="12-00-00",
                  sum3d_pending="11-00-00"),
        ])
        session.commit()

    def test_retries_only_pending_ids_with_the_letter(self, submitted):
        with make_session() as session:
            self._seed(session)
            assert wb.retry_pending_sum3d(session, now=1000.0) == 1
        fn, (order_id, fields) = submitted[0]
        assert fn is wb.write_sheet_fields_warm
        assert fields == {"sum3d_id", "calculated_raw"}

    def test_does_not_hammer_the_same_order(self, submitted):
        with make_session() as session:
            self._seed(session)
            wb.retry_pending_sum3d(session, now=1000.0)
            assert wb.retry_pending_sum3d(session, now=1000.0 + 30) == 0
            assert wb.retry_pending_sum3d(session, now=1000.0 + wb.PENDING_SUM3D_RETRY_SECONDS + 1) == 1

    def test_paused_sync_retries_nothing(self, submitted, monkeypatch):
        monkeypatch.setattr(wb.sync_control, "is_paused", lambda: True)
        with make_session() as session:
            self._seed(session)
            assert wb.retry_pending_sum3d(session, now=1000.0) == 0
        assert submitted == []


def test_the_queue_row_keeps_the_warning_after_the_poll(app_db):  # noqa: F811
    """Попередження — у ЗВИЧАЙНОМУ рендері черги (так рендерить полл), а не лише
    у відповіді на запис: саме його полл і прибирав."""
    from app.business_day import business_today

    app, session_factory = app_db
    tab = business_today().strftime("%d.%m.%y")
    with session_factory() as db:
        db.add_all([
            Order(source="lab", sheet_tab=tab, row_number=7, work_order_no="24122",
                  job_code="2026-09-10_00016-007", sum3d_id="16-27-26", sum3d_pending="16-27-26"),
            Order(source="lab", sheet_tab=tab, row_number=8, work_order_no="24123",
                  job_code="2026-09-10_00016-008", sum3d_id="16-30-00"),
        ])
        db.commit()
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/")
    assert status == 200, html[:500]
    assert html.count("is-sum3d-pending") == 1, "попередження мусить стояти рівно на недійшлому рядку"
    assert "Sum3D ще не в таблиці" in html
