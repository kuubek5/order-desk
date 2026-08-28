"""ActionLog: one row per state-changing operator action — the shared backbone
for Undo and the laconic action journal. Step 1 covers Sum3D + status logging.
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers.deps import attach_action_toast
from app.services.sheet_writeback import restore_sheet_row
from app.services.undo import UNDOABLE_ACTION_TYPES, log_action
from app.routers import orders as orders_router_mod
from app.services import sheet_writeback as writeback_service
from app.services import undo as undo_service
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
    with patch.object(orders_router_mod, "write_sheet_fields", return_value=None), \
         patch.object(orders_router_mod, "write_rework_sum3d_fields", return_value=None), \
         patch.object(orders_router_mod, "attach_export_folder_uris"), \
         patch.object(orders_router_mod, "attach_job_code_folder_uris"), \
         patch.object(web.templates, "TemplateResponse", return_value=SimpleNamespace(headers={})):
        asyncio.run(orders_router_mod.set_sum3d_id(
            request=_request(user.id), order_id=order.id, sum3d_id=value, db=db))


def _run_status(db, user, order, status):
    with patch.object(orders_router_mod, "write_sheet_fields", return_value=None), \
         patch.object(orders_router_mod, "attach_export_folder_uris"), \
         patch.object(orders_router_mod, "attach_job_code_folder_uris"), \
         patch.object(web.templates, "TemplateResponse", return_value=SimpleNamespace(headers={})):
        asyncio.run(orders_router_mod.set_status(
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
        log_action(db, order=order, operator=user, action_type="test",
                       field="quantity", old=None, new=5, note="п'ять")
        db.commit()
        e = db.scalar(select(ActionLog).where(ActionLog.action_type == "test"))
        assert e.new_value == "5"        # stringified
        assert e.old_value is None


# --- Undo (крок 2) -----------------------------------------------------------


def _run_undo(db, user, action_id):
    with patch.object(orders_router_mod, "write_sheet_fields", return_value=None), \
         patch.object(orders_router_mod, "write_rework_sum3d_fields", return_value=None):
        return asyncio.run(orders_router_mod.undo_action(
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
        order.status = "відфрезеровано"
        db.commit()
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
    with patch.object(orders_router_mod, "write_sheet_fields", return_value=None), \
         patch.object(orders_router_mod, "write_rework_sum3d_fields", return_value=None), \
         patch.object(orders_router_mod, "write_calculated_cell", return_value=None):
        return asyncio.run(orders_router_mod.undo_last_action(request=_request(user.id), db=db))


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


# --- Redo / «Крок вперед» ----------------------------------------------------


def _run_redo_last(db, user):
    with patch.object(orders_router_mod, "write_sheet_fields", return_value=None), \
         patch.object(orders_router_mod, "write_rework_sum3d_fields", return_value=None), \
         patch.object(orders_router_mod, "write_calculated_cell", return_value=None):
        return asyncio.run(orders_router_mod.redo_last_action(request=_request(user.id), db=db))


def test_redo_last_reapplies_undone_action():
    """«Крок вперед» re-applies the value the undone action originally set."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "у фрезеруванні")
        _run_undo_last(db, user)
        db.refresh(order)
        assert order.status == "прийнято"                # undone
        _run_redo_last(db, user)
        db.refresh(order)
        assert order.status == "у фрезеруванні"          # redone


def test_undo_redo_roundtrip_is_repeatable():
    """After a redo the entry is undoable again, so undo→redo→undo all step."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        order = _order(db, status="прийнято")
        _run_sum3d(db, user, order, "12-01-45")
        _run_undo_last(db, user)
        db.refresh(order)
        assert order.sum3d_id is None
        _run_redo_last(db, user)
        db.refresh(order)
        assert order.sum3d_id == "12-01-45"              # redone
        _run_undo_last(db, user)
        db.refresh(order)
        assert order.sum3d_id is None                    # undoable again after redo


def test_redo_refuses_if_value_changed_since_undo():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        order = _order(db, status="прийнято")
        _run_sum3d(db, user, order, "12-01-45")
        _run_undo_last(db, user)
        db.refresh(order)
        order.sum3d_id = "77-77-77"
        db.commit()         # someone set it after undo
        _run_redo_last(db, user)
        db.refresh(order)
        assert order.sum3d_id == "77-77-77"              # NOT clobbered by redo


def test_redo_with_nothing_to_redo_is_noop():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = _run_redo_last(db, user)
        assert resp.status_code == 204


# --- Видалення клітинки «Оператор» (calculated_raw / стовпець М) --------------


def _run_set_operator(db, user, order, value):
    with patch.object(orders_router_mod, "write_calculated_cell", return_value=None), \
         patch.object(orders_router_mod, "attach_export_folder_uris"), \
         patch.object(orders_router_mod, "attach_job_code_folder_uris"), \
         patch.object(web.templates, "TemplateResponse", return_value=SimpleNamespace(headers={})):
        return asyncio.run(orders_router_mod.set_operator(
            request=_request(user.id), order_id=order.id, operator=value, db=db))


def test_operator_set_writes_value_and_logs():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, calculated_raw=None)
        _run_set_operator(db, user, order, "St")
        db.refresh(order)
        assert order.calculated_raw == "St"                 # typed in manually
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "operator"))
        assert entry is not None and (entry.old_value or "") == "" and entry.new_value == "St"


def test_operator_clear_empties_cell_and_logs():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, calculated_raw="D")
        _run_set_operator(db, user, order, "")              # empty = clear
        db.refresh(order)
        assert not order.calculated_raw                     # cleared
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "operator"))
        assert entry is not None and entry.old_value == "D" and (entry.new_value or "") == ""


def test_operator_set_undo_restores_and_redo_reapplies():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, calculated_raw="ЕЕ")
        _run_set_operator(db, user, order, "К")             # change ЕЕ → К
        db.refresh(order)
        assert order.calculated_raw == "К"
        _run_undo_last(db, user)
        db.refresh(order)
        assert order.calculated_raw == "ЕЕ"                 # restored
        _run_redo_last(db, user)
        db.refresh(order)
        assert order.calculated_raw == "К"                  # re-applied


# --- Решта дій з таблицею: коментар, створення, видалення (крок 4) -----------


def _run_cam_comment(db, user, order, text):
    with patch.object(orders_router_mod, "write_sheet_fields_background"), \
         patch.object(orders_router_mod, "attach_export_folder_uris"), \
         patch.object(orders_router_mod, "attach_job_code_folder_uris"), \
         patch.object(web.templates, "TemplateResponse", return_value=SimpleNamespace(headers={})):
        asyncio.run(orders_router_mod.set_cam_comment(
            request=_request(user.id), order_id=order.id, cam_comment=text, db=db))


def test_cam_comment_is_logged_and_undoable():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, cam_comment="на швидку")
        _run_cam_comment(db, user, order, "покрити опаком")
        db.refresh(order)
        assert order.cam_comment == "покрити опаком"
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "cam_comment"))
        assert entry is not None and entry.old_value == "на швидку"
        _run_undo_last(db, user)
        db.refresh(order)
        assert order.cam_comment == "на швидку"          # reverted
        _run_redo_last(db, user)
        db.refresh(order)
        assert order.cam_comment == "покрити опаком"     # re-applied


def test_cam_comment_unchanged_is_not_logged():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, cam_comment="той самий")
        _run_cam_comment(db, user, order, "той самий")
        assert db.scalar(select(ActionLog).where(ActionLog.action_type == "cam_comment")) is None


def _run_delete(db, user, order):
    with patch.object(orders_router_mod, "clear_sheet_row_background"), \
         patch.object(undo_service, "restore_sheet_row", return_value=None):
        asyncio.run(orders_router_mod.delete_order(
            request=_request(user.id), order_id=order.id, inline="1", db=db))


def test_delete_is_logged_and_undo_restores_the_work():
    """A mis-clicked delete is the most valuable undo of all — it must bring the
    work back to the queue AND re-fill the sheet row it blanked."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, work_order_no="24122")
        _run_delete(db, user, order)
        db.refresh(order)
        assert order.archived_at is not None                # gone from the queue
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "delete"))
        assert entry is not None and "24122" in entry.note

        with patch.object(undo_service, "restore_sheet_row", return_value=None) as restore:
            _run_undo_last(db, user)
        db.refresh(order)
        assert order.archived_at is None                    # back in the queue
        assert restore.called                               # sheet row re-filled


def test_delete_undo_keeps_the_work_archived_when_the_row_cannot_be_restored():
    """Both halves or neither. If the sheet row can't come back (the lab re-used
    it), un-archiving anyway would leave the order pointing at somebody else's
    row — and sync_tab matches by row_number, so the next sync would overwrite
    the restored work with their data."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        _run_delete(db, user, order)
        db.refresh(order)
        archived_at = order.archived_at

        with patch.object(undo_service, "restore_sheet_row", return_value="рядок 9 уже зайнято"):
            _run_undo_last(db, user)
        db.refresh(order)
        assert order.archived_at == archived_at          # still deleted, not half-restored
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "delete"))
        assert entry.undone_at is None                   # still undoable — ← can be retried


def test_restore_sheet_row_goes_through_the_writeback_pool():
    """The delete blanks the row on the single-worker pool; the restore MUST queue
    behind it there, or it can land first and be wiped by the pending blank."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        order = _order(db)
        submitted = {}
        real_submit = web._sheet_writeback_pool.submit

        def spy(fn, *a, **kw):
            submitted["fn"] = fn
            return real_submit(fn, *a, **kw)

        with patch.object(writeback_service, "restore_sheet_row_warm", return_value=None), \
             patch.object(web._sheet_writeback_pool, "submit", side_effect=spy):
            assert restore_sheet_row(order) is None
        assert submitted.get("fn") is not None            # went through the pool


def test_delete_redo_archives_again():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        _run_delete(db, user, order)
        with patch.object(undo_service, "restore_sheet_row", return_value=None):
            _run_undo_last(db, user)
        db.refresh(order)
        assert order.archived_at is None
        with patch.object(orders_router_mod, "clear_sheet_row_background"):
            _run_redo_last(db, user)
        db.refresh(order)
        assert order.archived_at is not None                # deleted again


def _recent_context(db, user, tab=""):
    """Call the «Останні дії» popup route and capture the template context."""
    captured = {}

    def _fake(request, name, ctx):
        captured.update(ctx)
        captured["_template"] = name
        return SimpleNamespace(headers={})

    with patch.object(web.templates, "TemplateResponse", side_effect=_fake):
        orders_router_mod.get_recent_actions(request=_request(user.id), tab=tab, db=db)
    return captured


def test_recent_actions_lists_own_newest_first():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "прораховано")
        _run_status(db, user, order, "у фрезеруванні")
        ctx = _recent_context(db, user)
        notes = [e.note for e in ctx["entries"]]
        assert ctx["_template"] == "_actions_recent.html"
        assert notes[0].endswith("у фрезеруванні")   # newest first
        assert len(notes) == 2


def test_recent_actions_excludes_other_operators():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        other = _user(db, initial="К", username="kostya")
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "прораховано")
        # The popup is scoped to the viewer, so undo (own-only) and this list
        # can never disagree about what is steppable.
        assert _recent_context(db, other)["entries"] == []
        assert len(_recent_context(db, user)["entries"]) == 1


def test_recent_actions_keeps_undone_entries_visible():
    """An undone action stays in the list (struck through) — it is still a place
    the operator was, and «Крок вперед» can re-apply it."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "прораховано")
        _run_undo_last(db, user)
        entries = _recent_context(db, user)["entries"]
        assert len(entries) == 1 and entries[0].undone_at is not None


def test_recent_actions_capped_at_limit():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, calculated_raw=None)
        for i in range(orders_router_mod.RECENT_ACTIONS_LIMIT + 4):
            _run_set_operator(db, user, order, f"O{i}")
        entries = _recent_context(db, user)["entries"]
        assert len(entries) == orders_router_mod.RECENT_ACTIONS_LIMIT


def test_recent_actions_includes_manually_created_works():
    """Regression: the popup filtered by UNDOABLE_ACTION_TYPES, which excludes
    "create", so a work the operator had just added by hand never showed up —
    the exact symptom reported. It is listed (and locatable) but the arrows still
    skip it, since undoing a creation would delete a shared-sheet row."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, work_order_no="24122")
        log_action(db, order=order, operator=user, action_type="create",
                       note="додано вручну: 24122")
        db.commit()

        entries = _recent_context(db, user)["entries"]
        assert [e.action_type for e in entries] == ["create"]
        assert "create" in orders_router_mod.RECENT_ACTION_TYPES
        assert "create" not in UNDOABLE_ACTION_TYPES   # listed, not steppable


def test_undo_last_skips_creations():
    """«Крок назад» must not delete a sheet row behind a one-click arrow."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        log_action(db, order=order, operator=user, action_type="create", note="додано")
        db.commit()
        resp = _run_undo_last(db, user)
        assert resp.status_code == 204
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "create"))
        assert entry.undone_at is None                     # untouched


def test_recent_actions_ignores_undo_meta_entries():
    """undo/redo write their own ActionLog rows; the popup lists the real edits,
    not the bookkeeping, so stepping back doesn't spam the list."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, status="прийнято")
        _run_status(db, user, order, "прораховано")
        _run_undo_last(db, user)
        types = {e.action_type for e in _recent_context(db, user)["entries"]}
        assert types == {"status"}       # no "undo" row in the list


def test_operator_set_noop_when_unchanged():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, calculated_raw="D")
        _run_set_operator(db, user, order, "D")             # same value
        assert db.scalar(select(ActionLog).where(ActionLog.action_type == "operator")) is None


# --- Журнал: окремий екран + картка (крок 3) ---------------------------------


def test_journal_lists_newest_first_and_filters_by_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        roma = _user(db, username="roma")
        kostya = _user(db, username="kostya")
        order = _order(db)
        log_action(db, order=order, operator=roma, action_type="status",
                       field="status", old="нове", new="прийнято", note="A")
        log_action(db, order=order, operator=kostya, action_type="status",
                       field="status", old="прийнято", new="прораховано", note="B")
        db.commit()

        # No filter → both, newest first.
        resp = orders_router_mod.get_journal(request=_request(roma.id), db=db)
        entries = resp.context["entries"]
        assert [e.note for e in entries] == ["B", "A"]

        # Filter by operator → only theirs.
        resp = orders_router_mod.get_journal(request=_request(roma.id), operator=str(roma.id), db=db)
        assert [e.note for e in resp.context["entries"]] == ["A"]


def test_journal_filters_by_day():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        # значення не потрібне — важливий сам запис у журнал
        log_action(db, order=order, operator=user, action_type="status",
                       field="status", old="нове", new="прийнято", note="today")
        db.commit()
        e_old = log_action(db, order=order, operator=user, action_type="status",
                               field="status", old="прийнято", new="прораховано", note="old")
        db.commit()
        # Backdate one entry to a known past day.
        e_old.created_at = datetime(2026, 1, 15, 10, 0, 0)
        db.commit()

        resp = orders_router_mod.get_journal(request=_request(user.id), day="2026-01-15", db=db)
        notes = [e.note for e in resp.context["entries"]]
        assert notes == ["old"]


def test_order_detail_context_includes_actions():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        log_action(db, order=order, operator=user, action_type="sum3d",
                       field="sum3d_id", old="{}", new="{}", note="Sum3D → x")
        db.commit()
        resp = orders_router_mod.get_order_detail(request=_request(user.id), order_id=order.id, db=db)
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
    attach_action_toast(resp, SimpleNamespace(id=7), "Sum3D очищено")
    header = resp.headers["HX-Trigger"]
    header.encode("latin-1")   # must not raise
    assert "undoUrl" not in header
    assert "/actions/7/undo" not in header
