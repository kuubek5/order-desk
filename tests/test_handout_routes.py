"""POST /handout/issue-group: closes a client's handout card in one click —
every found order flips to "видано" and sheet_client rows get their blue
fill cleared in the sheet. Mocks the sheet layer (open_spreadsheet,
get_worksheet_by_name, clear_row_fills) so no real network is touched."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Order, StatusEvent, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    user = User(username="root", password_hash="unused", full_name="Роман", role="адмін")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, client=SimpleNamespace(host="127.0.0.1"))


YESTERDAY = (date.today() - timedelta(days=1)).strftime("%d.%m.%y")


def _client_order(client_name="Basarab", status="знайдено при видачі", row_number=60, sheet_tab=YESTERDAY):
    return Order(
        source="sheet_client", sheet_tab=sheet_tab, row_number=row_number,
        client_name=client_name, material_color="Ti", quantity="1", status=status,
    )


def _stub_sheet(monkeypatch, sheet_id=42):
    """Fake worksheet/spreadsheet so _write_sheet_fields and clear_row_fills
    both resolve without hitting the network; captures clear_row_fills calls."""
    fake_ws = SimpleNamespace(id=sheet_id, title=YESTERDAY)
    fake_ws.acell = lambda a1: SimpleNamespace(value="")
    fake_ws.batch_update = MagicMock()
    monkeypatch.setattr(web, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(web, "get_worksheet_by_name", lambda ss, name: fake_ws)
    captured = {}

    def fake_clear(spreadsheet, rows):
        captured["rows"] = rows

    monkeypatch.setattr(web, "clear_row_fills", fake_clear)
    return captured


def test_requires_authentication():
    engine = _database()
    with Session(engine) as db, pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(web.issue_handout_group(request=_request(None), client_name="X", db=db))
    assert exc.value.status_code == 401


def test_refuses_when_not_all_found(monkeypatch):
    engine = _database()
    _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="нове"))
        db.commit()

        import asyncio
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                web.issue_handout_group(request=_request(user.id), client_name="Basarab", db=db)
            )
        assert exc.value.status_code == 400
        # nothing flipped to видано
        order = db.scalar(select(Order))
        assert order.status == "нове"


def test_issues_group_and_clears_blue_fill(monkeypatch):
    engine = _database()
    captured = _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        o1 = _client_order(row_number=60)
        o2 = _client_order(row_number=61)
        db.add_all([o1, o2])
        db.commit()

        import asyncio
        resp = asyncio.run(
            web.issue_handout_group(request=_request(user.id), client_name="Basarab", db=db)
        )
        assert resp.status_code == 303

        orders = db.scalars(select(Order)).all()
        assert all(o.status == "видано" for o in orders)
        events = db.scalars(select(StatusEvent)).all()
        assert len(events) == 2
        assert all(e.status == "видано" and e.actor == "root" for e in events)

        # both rows cleared, absolute sheet row = row_number + HEADER_ROWS
        from app.parser import HEADER_ROWS
        assert sorted(captured["rows"]) == [
            (42, 60 + HEADER_ROWS), (42, 61 + HEADER_ROWS)
        ]


def test_already_issued_orders_are_left_alone_but_still_pass(monkeypatch):
    """A group that's already fully видано (button clicked twice, or a race)
    is a no-op — no duplicate StatusEvent, no sheet write."""
    engine = _database()
    _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="видано"))
        db.commit()

        import asyncio
        resp = asyncio.run(
            web.issue_handout_group(request=_request(user.id), client_name="Basarab", db=db)
        )
        assert resp.status_code == 303
        # order already excluded from candidates (status != "видано" filter)
        assert db.scalar(select(StatusEvent)) is None


def test_lab_orders_in_group_are_not_sent_to_clear_row_fills(monkeypatch):
    """A "lab" order (наряд, not a наряд-less client row) has no blue fill to
    clear — only sheet_client rows go into the clear batch."""
    engine = _database()
    captured = _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        lab_order = Order(
            source="lab", sheet_tab=YESTERDAY, row_number=7, work_order_no="24999",
            client_name="Basarab", material_color="Ti", quantity="1",
            status="знайдено при видачі",
        )
        db.add(lab_order)
        db.commit()

        import asyncio
        asyncio.run(
            web.issue_handout_group(request=_request(user.id), client_name="Basarab", db=db)
        )
        assert "rows" not in captured or captured["rows"] == []


def test_sheet_failure_still_marks_issued_and_flashes_error(monkeypatch):
    """A Sheets write hiccup must never block the видано status — the DB is
    the source of truth and the operator gets a visible warning instead."""
    engine = _database()
    _stub_sheet(monkeypatch)

    def boom(spreadsheet, rows):
        raise RuntimeError("проксі впав")

    monkeypatch.setattr(web, "clear_row_fills", boom)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order())
        db.commit()

        request = _request(user.id)
        import asyncio
        resp = asyncio.run(web.issue_handout_group(request=request, client_name="Basarab", db=db))
        assert resp.status_code == 303
        assert db.scalar(select(Order)).status == "видано"
        assert request.session["handout_flash"]["kind"] == "error"


def test_group_disappears_from_handout_listing_after_issue(monkeypatch):
    engine = _database()
    _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order())
        db.commit()

        import asyncio
        asyncio.run(
            web.issue_handout_group(request=_request(user.id), client_name="Basarab", db=db)
        )

        with patch.object(web, "scan_export_folder", return_value=[]):
            ctx_holder = {}
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: ctx_holder.update(context) or context,
            )
            web.get_handout(request=_request(user.id), db=db)
        assert ctx_holder["client_groups"] == []
