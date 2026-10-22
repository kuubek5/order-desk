"""«Відкрити в таблиці ↗» — посилання на вкладку й рядок роботи (22.10.26).

gid вкладки береться з памʼяті (листинг синку, `get_worksheet_by_name`), а не
окремим зверненням до Google на кожен рендер. Невідомий gid — посилання
немає, а не посилання на першу-ліпшу вкладку.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.sheets as sheets
from app.models import Order
from app.services import sheet_stuck_writes as stuck
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

SHEET = "1TESTSHEETIDxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


@pytest.fixture(autouse=True)
def _clean():
    sheets._tab_gids.clear()
    stuck.reset()
    yield
    sheets._tab_gids.clear()
    stuck.reset()


def _remember(tab: str, gid: int) -> None:
    sheets.remember_tab_gid(SimpleNamespace(id=SHEET), SimpleNamespace(id=gid, title=tab))


def test_url_points_at_tab_and_row():
    _remember("20.10.26", 123456)
    assert sheets.sheet_row_url(SHEET, "20.10.26", 80) == (
        f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid=123456&range=80:80"
    )


def test_unknown_gid_or_other_sheet_gives_no_link():
    _remember("20.10.26", 1)
    assert sheets.sheet_row_url(SHEET, "21.10.26", 80) is None
    assert sheets.sheet_row_url("іншаТаблиця", "20.10.26", 80) is None


def test_padded_title_is_remembered_under_the_canonical_name():
    _remember(" 08.09.26", 7)
    assert sheets.sheet_row_url(SHEET, "08.09.26", 9).endswith("gid=7&range=9:9")


def test_worksheet_lookup_remembers_the_gid(monkeypatch):
    ws = SimpleNamespace(id=55, title="20.10.26")
    spreadsheet = SimpleNamespace(id=SHEET, worksheet=lambda name: ws)
    monkeypatch.setattr(sheets, "repair_padded_tab_title", lambda s, w: None)
    monkeypatch.setattr(sheets._local, "worksheet_cache", {}, raising=False)
    assert sheets.get_worksheet_by_name(spreadsheet, "20.10.26") is ws
    assert sheets.sheet_row_url(SHEET, "20.10.26", None).endswith("#gid=55")


def _seed(session_factory):
    from app.settings_store import set_setting

    with session_factory() as db:
        set_setting(db, "google_sheet_id", SHEET)
        order = Order(source="email", sheet_tab="20.10.26", row_number=74, client_name="Клієнт-А",
                      material_color="mono a2", quantity="2", status="знайдено при видачі",
                      fill_pending="clear")
        db.add(order)
        db.commit()
        stuck.note_write_failed("fill", order, "рядок у таблиці не підтверджено — не записано")
        return order.id


def test_banner_and_passport_carry_the_link_without_calling_google(app_db, monkeypatch):  # noqa: F811
    from app.parser import HEADER_ROWS

    def no_google(*a, **k):
        raise AssertionError("рендер не ходить у Google")

    monkeypatch.setattr(sheets, "open_spreadsheet", no_google)
    app, session_factory = app_db
    order_id = _seed(session_factory)
    _remember("20.10.26", 9001)
    expected = f"edit#gid=9001&amp;range={74 + HEADER_ROWS}:{74 + HEADER_ROWS}"

    client = MiniClient(app)
    client.login(*OPERATOR)
    for path in ("/sheets/write-failures", "/"):
        _, _, html = client.get(path)
        assert "swf-sheet-link" in html and expected in html, path
    _, _, html = client.get(f"/orders/{order_id}")
    assert "Відкрити в таблиці ↗" in html and expected in html


def test_no_link_while_the_gid_is_unknown(app_db):  # noqa: F811
    app, session_factory = app_db
    _seed(session_factory)
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, html = client.get("/sheets/write-failures")
    assert "swf-banner" in html and "swf-sheet-link" not in html
