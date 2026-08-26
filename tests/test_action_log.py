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


def _run_undo_last(db, user):
    with patch.object(web, "_write_sheet_fields", return_value=None), \
         patch.object(web, "_write_rework_sum3d", return_value=None):
        return asyncio.run(web.undo_last_action(request=_request(user.id), db=db))


def test_undo_last_reverts_most_recent_action():
    """The static «Крок назад» button undoes the operator's latest action without
    being handed a specific id."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "у фрезеруванні")
        _run_undo_last(db, user)
        db.refresh(order)
        assert order.status == "прийнято"


def test_undo_last_steps_back_through_actions():
    """Pressing «Крок назад» again reverts the previous action too."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="нове")
        _run_status(db, user, order, "прийнято")
        _run_status(db, user, order, "у фрезеруванні")
        _run_undo_last(db, user)                       # undo the 2nd
        db.refresh(order)
        assert order.status == "прийнято"
        _run_undo_last(db, user)                       # step back once more
        db.refresh(order)
        assert order.status == "нове"


def test_undo_last_with_nothing_to_undo_is_noop():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = _run_undo_last(db, user)
        assert resp.status_code == 204
        assert "undone_at" not in ""  # no entries exist; nothing changed
        assert db.scalar(select(ActionLog)) is None


def test_undo_last_only_sees_own_actions():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        other = _user(db, initial="К", username="kostya")
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "у фрезеруванні")   # user's action
        _run_undo_last(db, other)                        # other has nothing
        db.refresh(order)
        assert order.status == "у фрезеруванні"          # untouched


# --- Журнал: окремий екран + картка (крок 3) ---------------------------------


def test_journal_lists_newest_first_and_filters_by_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        roma = _user(db, username="roma")
        kostya = _user(db, username="kostya")
        order = _order(db)
        web.log_action(db, order=order, operator=roma, action_type="status",
                       field="status", old="нове", new="прийнято", note="A")
        web.log_action(db, order=order, operator=kostya, action_type="status",
                       field="status", old="прийнято", new="прораховано", note="B")
        db.commit()

        # No filter → both, newest first.
        resp = web.get_journal(request=_request(roma.id), db=db)
        entries = resp.context["entries"]
        assert [e.note for e in entries] == ["B", "A"]

        # Filter by operator → only theirs.
        resp = web.get_journal(request=_request(roma.id), operator=str(roma.id), db=db)
        assert [e.note for e in resp.context["entries"]] == ["A"]


def test_journal_filters_by_day():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        e_today = web.log_action(db, order=order, operator=user, action_type="status",
                                 field="status", old="нове", new="прийнято", note="today")
        db.commit()
        e_old = web.log_action(db, order=order, operator=user, action_type="status",
                               field="status", old="прийнято", new="прораховано", note="old")
        db.commit()
        # Backdate one entry to a known past day.
        e_old.created_at = datetime(2026, 1, 15, 10, 0, 0)
        db.commit()

        resp = web.get_journal(request=_request(user.id), day="2026-01-15", db=db)
        notes = [e.note for e in resp.context["entries"]]
        assert notes == ["old"]


def test_order_detail_context_includes_actions():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        web.log_action(db, order=order, operator=user, action_type="sum3d",
                       field="sum3d_id", old="{}", new="{}", note="Sum3D → x")
        db.commit()
        resp = web.get_order_detail(request=_request(user.id), order_id=order.id, db=db)
        actions = resp.context["actions"]
        assert len(actions) == 1 and actions[0].note == "Sum3D → x"


def test_action_toast_header_is_latin1_safe():
    """HTTP header values are latin-1; a Cyrillic toast message must ride as
    ASCII unicode escapes, not raw Cyrillic. Regression: ensure_ascii=False here
    500'd every Sum3D/status action whose message had Cyrillic (unit tests mocked
    the response, so only a real Response object catches it). The confirmation
    toast no longer carries an undoUrl — undo is the static «Крок назад» button."""
    from starlette.responses import Response
    resp = Response()
    web._attach_action_toast(resp, SimpleNamespace(id=7), "Sum3D очищено")
    header = resp.headers["HX-Trigger"]
    header.encode("latin-1")   # must not raise
    assert "undoUrl" not in header
    assert "/actions/7/undo" not in header
