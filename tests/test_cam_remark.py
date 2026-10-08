"""Зауваження CAM-оператора технікові (власник 08.10.26).

Лягає в «Коментар для CAM» (K) окремим червоним рядком «❗ …»; текст техніка
лишається; клітинка переносить текст, щоб зауваження читалось повністю.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.sheet_writer as sheet_writer
from app.models import ActionLog, Order
from app.parser import HEADER_ROWS
from app.services import cam_remark
from app.services import sheet_writeback as writeback_service
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")

NOW = datetime(2026, 10, 8, 4, 30)


def test_line_compose_and_read_back():
    line = cam_remark.remark_line("немає папки", "Роман", NOW)
    assert line == "❗ немає папки · Роман 08.10 04:30"
    cell = cam_remark.compose("на швидку, 46 зуб", line)
    assert cell == "на швидку, 46 зуб\n❗ немає папки · Роман 08.10 04:30"
    assert cam_remark.remark_of(cell) == "немає папки · Роман 08.10 04:30"
    # Нове зауваження ЗАМІНЮЄ старе, текст техніка лишається.
    other = cam_remark.compose(cell, cam_remark.remark_line("тонкі шийки", "Роман", NOW))
    assert other == "на швидку, 46 зуб\n❗ тонкі шийки · Роман 08.10 04:30"
    assert cam_remark.compose(other, None) == "на швидку, 46 зуб"
    assert cam_remark.remark_of("на швидку") is None
    assert cam_remark.compose(None, line) == line


def test_remark_start_counts_utf16_units():
    text = "😀 зуб\n❗ немає папки"
    # «😀» — дві одиниці UTF-16, далі « зуб\n» — 5.
    assert cam_remark.remark_start(text) == 2 + 5
    assert cam_remark.remark_start("лише текст") is None


def _fake_ws(live):
    spreadsheet = SimpleNamespace(batch_update=MagicMock())
    ws = SimpleNamespace(id=7, spreadsheet=spreadsheet, batch_update=MagicMock())
    ws.acell = lambda a1: SimpleNamespace(value=live)
    return ws


def test_write_remark_reads_the_live_cell_and_paints_only_its_line(monkeypatch):
    monkeypatch.setattr(sheet_writer, "_resolve_row", lambda ws, order: 20)
    ws = _fake_ws("технік дописав щойно")
    line = cam_remark.remark_line("немає папки", "Роман", NOW)
    combined = sheet_writer.write_remark(ws, Order(id=1), line)

    assert combined == "технік дописав щойно\n" + line
    (body,), _ = ws.spreadsheet.batch_update.call_args
    upd = body["requests"][0]["updateCells"]
    assert upd["range"]["startColumnIndex"] == sheet_writer.COL_CAM_COMMENT - 1
    assert upd["range"]["startRowIndex"] == 19
    cell = upd["rows"][0]["values"][0]
    assert cell["userEnteredValue"]["stringValue"] == combined
    runs = cell["textFormatRuns"]
    assert runs[0] == {"startIndex": 0, "format": {}}
    assert runs[1]["startIndex"] == len("технік дописав щойно\n")
    assert runs[1]["format"]["bold"] is True and runs[1]["format"]["foregroundColor"]["red"] > 0.5
    assert cell["userEnteredFormat"]["wrapStrategy"] == "WRAP"


def test_write_remark_refuses_unconfirmed_row(monkeypatch):
    monkeypatch.setattr(sheet_writer, "_resolve_row", lambda ws, order: None)
    ws = _fake_ws("x")
    with pytest.raises(RuntimeError):
        sheet_writer.write_remark(ws, Order(id=1), "❗ x")
    ws.spreadsheet.batch_update.assert_not_called()


def test_plain_comment_edit_keeps_the_remark_red(monkeypatch):
    """Оператор поправив коментар у черзі — значення пишеться звичайно, і без
    повернення формату червоний рядок став би чорним."""
    monkeypatch.setattr(sheet_writer, "_resolve_row", lambda ws, order: 20)
    ws = _fake_ws("")
    order = Order(id=1, row_number=14, cam_comment="коментар\n❗ немає папки · Роман 08.10 04:30")
    sheet_writer.write_order_fields(ws, order, {"cam_comment"})
    ws.batch_update.assert_called_once()
    (body,), _ = ws.spreadsheet.batch_update.call_args
    assert body["requests"][0]["updateCells"]["rows"][0]["values"][0]["textFormatRuns"][1]["startIndex"] == len("коментар\n")

    plain = _fake_ws("")
    sheet_writer.write_order_fields(plain, Order(id=2, row_number=14, cam_comment="без зауваження"), {"cam_comment"})
    plain.spreadsheet.batch_update.assert_not_called()


def _stub_sheet(monkeypatch, engine, live):
    ws = _fake_ws(live)
    monkeypatch.setattr(writeback_service, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(writeback_service, "get_worksheet_by_name", lambda ss, name: ws)
    monkeypatch.setattr(writeback_service, "writeback_session",
                        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(sheet_writer, "_resolve_row", lambda w, order: order.row_number + HEADER_ROWS)
    return ws


def test_route_writes_remark_and_renders_red_chip(app_db, monkeypatch):  # noqa: F811
    from app.business_day import business_today

    app, factory = app_db
    tab = business_today().strftime("%d.%m.%y")
    with factory() as db:
        order = Order(source="lab", sheet_tab=tab, row_number=5, work_order_no="34203",
                      material_color="моно а3", quantity="1", status="нове", cam_comment="на швидку")
        db.add(order)
        db.commit()
        oid = order.id
        engine = db.get_bind()
    ws = _stub_sheet(monkeypatch, engine, "на швидку, технік дописав")
    client = MiniClient(app)
    client.login(*OPERATOR)

    status, _, html = client.post(f"/orders/{oid}/remark", {"remark": "немає папки"},
                                  headers={"HX-Request": "true"})
    assert status == 200, html[:300]
    assert 'class="remark-badge"' in html and "немає папки" in html
    with factory() as db:
        saved = db.get(Order, oid)
        assert saved.cam_comment.startswith("на швидку, технік дописав\n❗ немає папки · ")
        assert db.scalar(select(ActionLog).where(ActionLog.action_type == "remark")) is not None
    ws.spreadsheet.batch_update.assert_called_once()

    status, _, html = client.post(f"/orders/{oid}/remark", {"remark": ""}, headers={"HX-Request": "true"})
    assert status == 200 and 'class="remark-badge"' not in html
    with factory() as db:
        assert db.get(Order, oid).cam_comment == "на швидку, технік дописав"
