"""Pause switch: while paused the app touches the Google Sheet in NEITHER
direction — the background read loop is skipped and every table-writing operator
action is refused (no DB change, so there's nothing for the resume-read to
revert). Mail is intentionally NOT paused: it doesn't touch the sheet."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers import orders as orders_router_mod
from app.routers import queue as queue_router_mod
from app import sync_control
from app.db import Base
from app.models import Order, User


@pytest.fixture(autouse=True)
def _resume_after_each():
    sync_control.resume()
    yield
    sync_control.resume()


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, role="оператор"):
    u = User(username="op", password_hash="x", full_name="Оп", role=role)
    db.add(u)
    db.commit()
    return u


def _request(user_id, role_admin=False, headers=None):
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers(headers or {}),
    )


def _order(db, **kw):
    d = dict(source="lab", sheet_tab="26.08.26", row_number=7,
             work_order_no="24122", status="нове")
    d.update(kw)
    o = Order(**d)
    db.add(o)
    db.commit()
    return o


def test_flag_defaults_to_running_and_toggles():
    assert sync_control.is_paused() is False
    sync_control.pause()
    assert sync_control.is_paused() is True
    sync_control.set_paused(False)
    assert sync_control.is_paused() is False


def test_set_sum3d_refused_while_paused_without_touching_db():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, sum3d_id=None)
        sync_control.pause()
        with patch.object(orders_router_mod, "write_sheet_fields") as write:
            asyncio.run(orders_router_mod.set_sum3d_id(
                request=_request(user.id), order_id=order.id,
                sum3d_id="12-01-45", db=db,
            ))
        write.assert_not_called()  # nothing written to the sheet
        assert db.get(Order, order.id).sum3d_id is None  # nor to the DB


def test_delete_refused_while_paused():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        sync_control.pause()
        with patch.object(orders_router_mod, "clear_sheet_row_background") as clear:
            asyncio.run(orders_router_mod.delete_order(
                request=_request(user.id, headers={"HX-Request": "true"}),
                order_id=order.id, inline="1", db=db,
            ))
        clear.assert_not_called()
        assert db.get(Order, order.id).archived_at is None  # not archived


def test_manual_add_refused_while_paused():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        sync_control.pause()
        with patch.object(web, "_sheet_writeback_pool") as pool:
            resp = orders_router_mod.create_manual_order(
                request=_request(user.id), work_type="client", db=db,
                client_name=["Неда"], material_color=["mono b1"],
                work_order_no=[], kind=[], quantity=[], sum3d_id=[],
                job_code=[], technician_name=[],
            )
        pool.submit.assert_not_called()
        assert resp.status_code == 303
        assert db.scalar(select(Order)) is None


def test_pause_toggle_route_is_admin_only():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        op = _user(db, role="оператор")
        with pytest.raises(HTTPException) as exc:
            queue_router_mod.toggle_sync_pause(request=_request(op.id), db=db)
        assert exc.value.status_code == 403
        assert sync_control.is_paused() is False


def test_pause_toggle_route_flips_state_for_admin():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _user(db, role="адмін")
        queue_router_mod.toggle_sync_pause(request=_request(admin.id), db=db)
        assert sync_control.is_paused() is True
        queue_router_mod.toggle_sync_pause(request=_request(admin.id), db=db)
        assert sync_control.is_paused() is False
