"""Operator sheet-letter feature: the portal stamps the logged-in operator's
1-2 letter initial into the sheet's "Прорахував" column (М for a normal work, Х
for a rework) when they enter a Sum3D ID, matching the lab's by-hand convention.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Order, ReworkRecord, StatusEvent, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, role="оператор", initial=None, username="op"):
    u = User(username=username, password_hash="x", full_name="Оп",
             role=role, sheet_initial=initial)
    db.add(u)
    db.commit()
    return u


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers({}),
    )


def _form_request(user_id, form):
    async def _form():
        return form
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        form=_form,
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


# --- settings: assign / validate the letter --------------------------------


def test_create_operator_stores_uppercased_initial():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _user(db, role="адмін", username="admin")
        asyncio.run(web.create_operator(
            request=_form_request(admin.id, {
                "username": "roma", "password": "pw", "full_name": "Рома",
                "role": "оператор", "sheet_initial": "р",
            }),
            db=db,
        ))
        roma = db.scalar(select(User).where(User.username == "roma"))
        assert roma.sheet_initial == "Р"  # normalized to upper


def test_set_operator_initial_route_updates_and_clears():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _user(db, role="адмін", username="admin")
        op = _user(db, username="kostya")
        asyncio.run(web.set_operator_initial(
            request=_form_request(admin.id, {"sheet_initial": "К"}), user_id=op.id, db=db))
        assert db.get(User, op.id).sheet_initial == "К"
        # empty clears it
        asyncio.run(web.set_operator_initial(
            request=_form_request(admin.id, {"sheet_initial": ""}), user_id=op.id, db=db))
        assert db.get(User, op.id).sheet_initial is None


def test_duplicate_initial_is_rejected():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _user(db, role="адмін", username="admin", initial="Р")
        op = _user(db, username="other")
        resp = asyncio.run(web.set_operator_initial(
            request=_form_request(admin.id, {"sheet_initial": "р"}), user_id=op.id, db=db))
        assert resp.status_code == 303
        assert "error" in resp.headers["location"]
        assert db.get(User, op.id).sheet_initial is None  # not set


def test_too_long_initial_is_rejected():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _user(db, role="адмін", username="admin")
        op = _user(db, username="stas")
        resp = asyncio.run(web.set_operator_initial(
            request=_form_request(admin.id, {"sheet_initial": "СТС"}), user_id=op.id, db=db))
        assert "error" in resp.headers["location"]
        assert db.get(User, op.id).sheet_initial is None


def test_set_initial_is_admin_only():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        op = _user(db, role="оператор", username="op1")
        other = _user(db, username="op2")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(web.set_operator_initial(
                request=_form_request(op.id, {"sheet_initial": "Х"}), user_id=other.id, db=db))
        assert exc.value.status_code == 403


# --- set_sum3d_id: stamp the letter into "Прорахував" ----------------------


def _run_sum3d(db, user, order, value):
    with patch.object(web, "_write_sheet_fields", return_value=None) as ws, \
         patch.object(web, "_write_rework_sum3d", return_value=None) as wr, \
         patch.object(web, "attach_export_folder_uris"), \
         patch.object(web, "attach_job_code_folder_uris"), \
         patch.object(web.templates, "TemplateResponse", return_value="ok"):
        asyncio.run(web.set_sum3d_id(
            request=_request(user.id), order_id=order.id, sum3d_id=value, db=db))
    return ws, wr


def test_sum3d_entry_stamps_operator_letter_in_column_M():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        order = _order(db, status="прийнято")
        ws, _ = _run_sum3d(db, user, order, "12-01-45")
        db.refresh(order)
        assert order.sum3d_id == "12-01-45"
        assert order.calculated_raw == "Р"       # letter written to М
        assert order.status == "прораховано"      # letter is the calc marker
        # sheet write carried both cells
        assert ws.call_args[0][2] == {"sum3d_id", "calculated_raw"}
        ev = db.scalars(select(StatusEvent).where(StatusEvent.status == "прораховано")).first()
        assert ev is not None and ev.operator_id == user.id  # real operator recorded


def test_sum3d_entry_without_a_letter_leaves_column_M_untouched():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial=None)
        order = _order(db, status="прийнято")
        ws, _ = _run_sum3d(db, user, order, "12-01-45")
        db.refresh(order)
        assert order.sum3d_id == "12-01-45"
        assert order.calculated_raw is None       # nothing stamped
        assert order.status == "прийнято"          # status not advanced
        assert ws.call_args[0][2] == {"sum3d_id"}  # only the ID written


def test_clearing_sum3d_does_not_stamp_a_letter():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        order = _order(db, status="прийнято")
        ws, _ = _run_sum3d(db, user, order, "")
        db.refresh(order)
        assert order.sum3d_id is None
        assert order.calculated_raw is None
        assert ws.call_args[0][2] == {"sum3d_id"}


def test_rework_sum3d_stamps_letter_in_column_X():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="В")
        order = _order(db, status="переробка")
        db.add(ReworkRecord(order_id=order.id, occurrence=2))
        db.commit()
        _, wr = _run_sum3d(db, user, order, "22-01-02")
        db.refresh(order)
        rework = order.active_rework
        assert rework.sum3d_id == "22-01-02"
        assert rework.calculated_raw == "В"        # letter into rework "Прорахував" (Х)
        # the write wrapper got the letter
        assert wr.call_args.kwargs.get("letter") == "В"


def test_write_rework_calculated_targets_column_X():
    import gspread.utils
    from app.sheet_writer import write_rework_calculated, COL_REDO_CALCULATED
    order = SimpleNamespace(id=1, row_number=1, source="lab", work_order_no="A",
                            client_name=None)
    ws = MagicMock()
    write_rework_calculated(ws, order, "В")
    # row 1 + HEADER_ROWS(6) = sheet row 7, column X (24)
    ws.update_cell.assert_called_once_with(7, COL_REDO_CALCULATED, "В")
    assert COL_REDO_CALCULATED == 24


# --- operator cabinet: self-service letter -----------------------------------


def test_account_initial_lets_operator_set_own_letter():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        op = _user(db, username="roma")
        with patch.object(web.templates, "TemplateResponse", return_value="ok"):
            asyncio.run(web.post_account_initial(
                request=_request(op.id), sheet_initial="р", db=db))
        assert db.get(User, op.id).sheet_initial == "Р"  # normalized, saved


def test_account_initial_rejects_letter_taken_by_another_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        other = _user(db, username="kostya", initial="К")
        op = _user(db, username="roma")
        with patch.object(web.templates, "TemplateResponse", return_value="err") as tr:
            asyncio.run(web.post_account_initial(
                request=_request(op.id), sheet_initial="к", db=db))
        # error surfaced, letter NOT set
        assert tr.call_args[0][2].get("error")
        assert db.get(User, op.id).sheet_initial is None


def test_account_initial_keeping_own_letter_is_allowed():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        op = _user(db, username="stas", initial="СТ")
        with patch.object(web.templates, "TemplateResponse", return_value="ok"):
            asyncio.run(web.post_account_initial(
                request=_request(op.id), sheet_initial="СТ", db=db))
        assert db.get(User, op.id).sheet_initial == "СТ"  # not a false "taken" clash
