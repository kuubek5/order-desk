"""ActionLog: one row per state-changing operator action — the shared backbone
for Undo and the laconic action journal. Step 1 covers Sum3D + status logging.
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import ActionLog, Order, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, initial=None, username="op"):
    u = User(username=username, password_hash="x", full_name="Оп",
             role="оператор", sheet_initial=initial)
    db.add(u)
    db.commit()
    return u


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers({}),
    )


def _order(db, **kw):
    d = dict(source="lab", sheet_tab="26.08.26", row_number=7,
             work_order_no="24122", status="прийнято")
    d.update(kw)
    o = Order(**d)
    db.add(o)
    db.commit()
    return o


def _run_sum3d(db, user, order, value):
    with patch.object(web, "_write_sheet_fields", return_value=None), \
         patch.object(web, "_write_rework_sum3d", return_value=None), \
         patch.object(web, "attach_export_folder_uris"), \
         patch.object(web, "attach_job_code_folder_uris"), \
         patch.object(web.templates, "TemplateResponse", return_value=SimpleNamespace(headers={})):
        asyncio.run(web.set_sum3d_id(
            request=_request(user.id), order_id=order.id, sum3d_id=value, db=db))


def _run_status(db, user, order, status):
    with patch.object(web, "_write_sheet_fields", return_value=None), \
         patch.object(web, "attach_export_folder_uris"), \
         patch.object(web, "attach_job_code_folder_uris"), \
         patch.object(web.templates, "TemplateResponse", return_value=SimpleNamespace(headers={})):
        asyncio.run(web.set_status(
            request=_request(user.id), order_id=order.id, status=status, db=db))


def test_sum3d_entry_is_logged():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        _run_sum3d(db, user, order, "12-01-45")
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "sum3d"))
        assert entry is not None
        assert entry.order_id == order.id
        assert entry.operator_id == user.id
        assert entry.field == "sum3d_id"
        # old/new are full JSON snapshots (Sum3D + letter + status) so undo can
        # revert everything the action touched.
        assert json.loads(entry.old_value)["sum3d_id"] is None       # was empty
        assert json.loads(entry.new_value)["sum3d_id"] == "12-01-45"
        assert "12-01-45" in entry.note
        assert entry.undone_at is None


def test_sum3d_clear_records_old_value_for_undo():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, sum3d_id="12-01-45", status="прораховано")
        _run_sum3d(db, user, order, "")   # operator clears it
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "sum3d"))
        assert json.loads(entry.old_value)["sum3d_id"] == "12-01-45"   # enough to undo
        assert json.loads(entry.new_value)["sum3d_id"] is None
        assert "очищено" in entry.note


def test_status_change_is_logged_with_old_and_new():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "у фрезеруванні")
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "status"))
        assert entry is not None
        assert entry.old_value == "прийнято"
        assert entry.new_value == "у фрезеруванні"
        assert entry.field == "status"


def test_status_set_to_same_value_is_not_logged():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "прийнято")   # no real change
        entries = list(db.scalars(select(ActionLog).where(ActionLog.action_type == "status")))
        assert entries == []   # no noise for a no-op


def test_log_action_helper_stringifies_values():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        web.log_action(db, order=order, operator=user, action_type="test",
                       field="quantity", old=None, new=5, note="п'ять")
        db.commit()
        e = db.scalar(select(ActionLog).where(ActionLog.action_type == "test"))
        assert e.new_value == "5"        # stringified
        assert e.old_value is None


# --- Undo (крок 2) -----------------------------------------------------------


def _run_undo(db, user, action_id):
    with patch.object(web, "_write_sheet_fields", return_value=None), \
         patch.object(web, "_write_rework_sum3d", return_value=None):
        return asyncio.run(web.undo_action(
            request=_request(user.id), action_id=action_id, db=db))


def test_undo_sum3d_reverts_value_letter_and_status():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        order = _order(db, status="прийнято")
        _run_sum3d(db, user, order, "12-01-45")      # sets sum3d, letter Р, status прораховано
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "sum3d"))
        assert order.sum3d_id == "12-01-45" and order.status == "прораховано"

        _run_undo(db, user, entry.id)
        db.refresh(order)
        assert order.sum3d_id is None            # reverted
        assert order.calculated_raw is None      # letter reverted
        assert order.status == "прийнято"        # status reverted
        db.refresh(entry)
        assert entry.undone_at is not None       # marked undone


def test_undo_status_reverts_status():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "у фрезеруванні")
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "status"))
        _run_undo(db, user, entry.id)
        db.refresh(order)
        assert order.status == "прийнято"


def test_undo_refuses_if_value_changed_since():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        order = _order(db, status="прийнято")
        _run_sum3d(db, user, order, "12-01-45")
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "sum3d"))
        # Someone (technician in sheet / other op) changed Sum3D since.
        order.sum3d_id = "99-99-99"
        db.commit()
        _run_undo(db, user, entry.id)
        db.refresh(order)
        assert order.sum3d_id == "99-99-99"      # NOT clobbered
        db.refresh(entry)
        assert entry.undone_at is None           # refused


def test_undo_only_by_the_operator_who_did_it():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        other = _user(db, initial="К", username="kostya")
        order = _order(db, status="прийнято")
        _run_sum3d(db, user, order, "12-01-45")
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "sum3d"))
        _run_undo(db, other, entry.id)           # different operator
        db.refresh(order)
        assert order.sum3d_id == "12-01-45"      # unchanged
        db.refresh(entry)
        assert entry.undone_at is None


def test_undo_cannot_run_twice():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "відфрезеровано")
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "status"))
        _run_undo(db, user, entry.id)
        db.refresh(order)
        assert order.status == "прийнято"
        # change it again, then try to undo the SAME entry — must refuse
        order.status = "відфрезеровано"; db.commit()
        _run_undo(db, user, entry.id)
        db.refresh(order)
        assert order.status == "відфрезеровано"  # second undo did nothing


def test_undo_logs_an_undo_action():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "у фрезеруванні")
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "status"))
        _run_undo(db, user, entry.id)
        undo_entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "undo"))
        assert undo_entry is not None
        assert "скасовано" in undo_entry.note
