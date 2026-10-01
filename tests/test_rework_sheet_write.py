"""Sum3D переробки в таблицю — одна звірка рядка й один запис (30.09.26).

Заміри з цеху: Sum3D переробки чекав на Google ~1.7 с, бо робив дві пари
«звірка рядка + update_cell» (W, потім X). Тепер `write_rework_sum3d_fields`
іде через `write_rework_cells`. Стережемо, що сервіс кличе саме його і що
журнал синку й текст помилки лишились тими самими, що й до зміни.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models import Order, SyncLog
from app.services import sheet_writeback as wb
from app.sync import sync_tab
from tests.test_sync import make_row, make_session

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _lab_order(session) -> Order:
    sync_tab(session, "T", [make_row()])
    session.commit()
    return session.scalar(select(Order))


def _patch_sheet(monkeypatch, ws):
    # Підміна мусить влучити в імена, якими користується сам сервіс (CLAUDE.md
    # §14: після переносу коду monkeypatch мовчки стає no-op).
    monkeypatch.setattr(wb, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(wb, "get_worksheet_by_name", lambda _sp, _name: ws)


def test_confirmed_row_writes_both_cells_in_one_call_and_logs_ok(monkeypatch):
    with make_session() as session:
        order = _lab_order(session)
        ws = MagicMock()
        ws.cell.return_value = SimpleNamespace(value=order.work_order_no)
        _patch_sheet(monkeypatch, ws)

        error = wb.write_rework_sum3d_fields(session, order, "17-05-21", "K")

        assert error is None
        assert ws.cell.call_count == 1
        ws.batch_update.assert_called_once()
        ws.update_cell.assert_not_called()
        session.flush()
        log = session.scalar(select(SyncLog).order_by(SyncLog.id.desc()))
        assert log.status == "ok"
        assert log.message == f"order {order.id}: rework sum3d_id"


def test_unconfirmed_row_is_reported_as_skipped_not_ok(monkeypatch):
    with make_session() as session:
        order = _lab_order(session)
        ws = MagicMock()
        ws.cell.return_value = SimpleNamespace(value="чужий наряд")
        ws.col_values.return_value = []
        _patch_sheet(monkeypatch, ws)

        error = wb.write_rework_sum3d_fields(session, order, "17-05-21", "K")

        assert error == "рядок у таблиці не підтверджено — не записано, спробуйте ще раз"
        ws.batch_update.assert_not_called()
        session.flush()
        log = session.scalar(select(SyncLog).order_by(SyncLog.id.desc()))
        assert log.status == "skipped"


def test_non_lab_work_is_not_written(monkeypatch):
    """Той самий гейт, що й до зміни: переробка пишеться лише в лабораторний рядок."""
    with make_session() as session:
        order = _lab_order(session)
        order.source = "sheet_client"
        ws = MagicMock()
        _patch_sheet(monkeypatch, ws)

        assert wb.write_rework_sum3d_fields(session, order, "17-05-21", "K") is None
        ws.batch_update.assert_not_called()
        ws.cell.assert_not_called()
