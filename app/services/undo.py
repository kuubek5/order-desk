"""Журнал дій, «Крок назад» і «Крок вперед».

Дія тут відокремлена від відповіді: `perform_undo`/`perform_redo` повертають
`UndoOutcome` — що сталось і що сказати операторові, — а перетворення цього на
HTTP-відповідь (тост, HX-Trigger) лишається в HTTP-шарі. Тому саме скасування
можна викликати й перевіряти без запиту.
"""

from dataclasses import dataclass
from datetime import datetime
import json

from sqlalchemy.orm import Session

from app.models import ActionLog, Order, StatusEvent, User
from app.services.sheet_writeback import (
    clear_sheet_row_background,
    restore_sheet_row,
    write_calculated_cell,
    write_rework_sum3d_fields,
    write_sheet_fields,
)

# How long after an action "Скасувати" stays valid. The toast lives ~15s, but a
# direct call could arrive later; a short window keeps undo from clobbering work
# others may have built on top of a change made long ago.
UNDO_WINDOW_SECONDS = 5 * 60

UNDOABLE_ACTION_TYPES = ("sum3d", "status", "operator", "cam_comment", "delete")


@dataclass(frozen=True)
class UndoOutcome:
    """Що сказати операторові про спробу скасування/повтору.

    `refresh_queue` — чи є сенс негайно перечитати чергу: ставиться лише тоді,
    коли дані справді змінились, щоб відмова («значення вже змінили») не
    смикала екран даремно."""

    message: str
    kind: str = "success"  # success | error | info
    refresh_queue: bool = False


def log_action(
    db: Session,
    *,
    order: "Order | None",
    operator: "User | None",
    action_type: str,
    field: str | None = None,
    old=None,
    new=None,
    note: str | None = None,
) -> ActionLog:
    """Record one state-CHANGING operator action into the ActionLog — the shared
    backbone for "Скасувати" (undo) and the laconic action journal. Call it
    inside the route's own transaction (committed together with the change), AND
    only for actions that actually changed data — never for reads/navigation.

    field/old/new capture enough to undo (restore the old value); note is the
    pre-rendered one-line journal summary. Values are stringified so any column
    type logs uniformly."""
    entry = ActionLog(
        order_id=order.id if order is not None else None,
        operator_id=operator.id if operator is not None else None,
        action_type=action_type,
        field=field,
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new),
        note=note,
    )
    db.add(entry)
    return entry


def snapshot_target(order: Order, field: str):
    """Resolve a snapshot field name to (object, attr). "rework.x" targets the
    order's active rework record; everything else targets the order itself."""
    if field.startswith("rework."):
        return order.active_rework, field.split(".", 1)[1]
    return order, field


def perform_undo(db: Session, user: User, entry: ActionLog) -> UndoOutcome:
    """Revert one already-validated ActionLog entry to its before-state (Sum3D or
    status), writing the old value back to DB + sheet. The caller has already
    checked ownership, the not-yet-undone flag and the time window; this does the
    revert, the concurrent-change guard, the log and the commit. On success it
    also asks for a queue refresh so the polled queue shows the reverted row at
    once instead of after the next 15s tick."""
    order = db.get(Order, entry.order_id) if entry.order_id else None
    if order is None:
        return UndoOutcome("Роботи більше немає — скасувати не можна", kind="error")

    if entry.action_type == "sum3d":
        before = json.loads(entry.old_value or "{}")
        after = json.loads(entry.new_value or "{}")
        # Guard: every field this action set must still hold what it set — else
        # someone changed it since and undo would clobber them.
        for f, v in after.items():
            obj, attr = snapshot_target(order, f)
            if obj is None or getattr(obj, attr) != v:
                return UndoOutcome("Не можна скасувати — значення вже змінили", kind="error")
        # Restore the whole before-snapshot.
        for f, v in before.items():
            obj, attr = snapshot_target(order, f)
            if obj is not None:
                setattr(obj, attr, v)
        if entry.field == "rework.sum3d_id":
            rework = order.active_rework
            restored = (rework.sum3d_id if rework else None) or ""
            restored_letter = (rework.calculated_raw if rework else None)
            sync_error = write_rework_sum3d_fields(db, order, restored, letter=restored_letter)
        else:
            sync_error = write_sheet_fields(db, order, {"sum3d_id", "calculated_raw"})
            db.add(StatusEvent(
                order_id=order.id, operator_id=user.id, status=order.status,
                actor=user.username, note="скасовано (Sum3D)",
            ))
    elif entry.action_type == "status":
        if order.status != entry.new_value:
            return UndoOutcome("Не можна скасувати — статус уже змінили", kind="error")
        order.status = entry.old_value
        db.add(StatusEvent(
            order_id=order.id, operator_id=user.id, status=order.status,
            actor=user.username, note="скасовано (статус)",
        ))
        sync_error = None
    elif entry.action_type == "operator":
        if (order.calculated_raw or "") != (entry.new_value or ""):
            return UndoOutcome("Не можна скасувати — оператора вже змінили", kind="error")
        order.calculated_raw = entry.old_value or ""
        sync_error = write_calculated_cell(db, order, order.calculated_raw)
    elif entry.action_type == "cam_comment":
        if (order.cam_comment or "") != (entry.new_value or ""):
            return UndoOutcome("Не можна скасувати — коментар уже змінили", kind="error")
        order.cam_comment = entry.old_value or None
        sync_error = write_sheet_fields(db, order, {"cam_comment"})
    elif entry.action_type == "delete":
        if order.archived_at is None:
            return UndoOutcome("Робота вже повернута в чергу", kind="info")
        # Restore the sheet row FIRST, and un-archive only if it actually came
        # back. Both halves or neither: an order returned to the queue whose
        # row_number now points at somebody else's row gets silently overwritten
        # with their data by the next sync (sync_tab matches by row_number). On
        # failure the entry stays not-undone, so ← can simply be pressed again
        # once the sheet is sorted out.
        restore_error = restore_sheet_row(order)
        if restore_error:
            return UndoOutcome(
                "Не вдалося відновити рядок у таблиці: " + restore_error, kind="error"
            )
        order.archived_at = None
        sync_error = None
        db.add(StatusEvent(
            order_id=order.id, operator_id=user.id, status=order.status,
            actor=user.username, note="відновлено з видалення",
        ))
    else:
        return UndoOutcome("Цей тип дії поки не скасовується", kind="error")

    entry.undone_at = datetime.utcnow()
    log_action(
        db, order=order, operator=user, action_type="undo",
        field=entry.field, note=f"скасовано: {entry.note}",
    )
    db.commit()

    if sync_error:
        return UndoOutcome(
            "Скасовано в системі, але таблиця не оновилась: " + sync_error,
            kind="error", refresh_queue=True,
        )
    return UndoOutcome("Дію скасовано", refresh_queue=True)


def perform_redo(db: Session, user: User, entry: ActionLog) -> UndoOutcome:
    """Re-apply an already-undone action — the mirror of perform_undo. Restores
    the entry's AFTER-state (the value the action originally set), guards that
    nothing has changed since the undo (else the redo would clobber someone), then
    clears `undone_at` so the entry is undoable again. Symmetric with undo, so
    «Крок назад» / «Крок вперед» form a proper step chain."""
    order = db.get(Order, entry.order_id) if entry.order_id else None
    if order is None:
        return UndoOutcome("Роботи більше немає — повторити не можна", kind="error")

    if entry.action_type == "sum3d":
        before = json.loads(entry.old_value or "{}")
        after = json.loads(entry.new_value or "{}")
        # Guard: the value must still be in the reverted (before) state — else
        # something changed since the undo and a redo would clobber it.
        for f, v in before.items():
            obj, attr = snapshot_target(order, f)
            if obj is None or getattr(obj, attr) != v:
                return UndoOutcome("Не можна повторити — значення вже змінили", kind="error")
        for f, v in after.items():
            obj, attr = snapshot_target(order, f)
            if obj is not None:
                setattr(obj, attr, v)
        if entry.field == "rework.sum3d_id":
            rework = order.active_rework
            restored = (rework.sum3d_id if rework else None) or ""
            restored_letter = (rework.calculated_raw if rework else None)
            sync_error = write_rework_sum3d_fields(db, order, restored, letter=restored_letter)
        else:
            sync_error = write_sheet_fields(db, order, {"sum3d_id", "calculated_raw"})
            db.add(StatusEvent(
                order_id=order.id, operator_id=user.id, status=order.status,
                actor=user.username, note="повторено (Sum3D)",
            ))
    elif entry.action_type == "status":
        if order.status != entry.old_value:
            return UndoOutcome("Не можна повторити — статус уже змінили", kind="error")
        order.status = entry.new_value
        db.add(StatusEvent(
            order_id=order.id, operator_id=user.id, status=order.status,
            actor=user.username, note="повторено (статус)",
        ))
        sync_error = None
    elif entry.action_type == "operator":
        if (order.calculated_raw or "") != (entry.old_value or ""):
            return UndoOutcome("Не можна повторити — оператора вже змінили", kind="error")
        order.calculated_raw = entry.new_value or ""
        sync_error = write_calculated_cell(db, order, order.calculated_raw)
    elif entry.action_type == "cam_comment":
        if (order.cam_comment or "") != (entry.old_value or ""):
            return UndoOutcome("Не можна повторити — коментар уже змінили", kind="error")
        order.cam_comment = entry.new_value or None
        sync_error = write_sheet_fields(db, order, {"cam_comment"})
    elif entry.action_type == "delete":
        if order.archived_at is not None:
            return UndoOutcome("Робота вже видалена", kind="info")
        order.archived_at = datetime.utcnow()
        if order.source in ("lab", "sheet_client") and order.sheet_tab and order.row_number:
            clear_sheet_row_background(order.sheet_tab, order.row_number)
        sync_error = None
        db.add(StatusEvent(
            order_id=order.id, operator_id=user.id, status=order.status,
            actor=user.username, note="видалено з черги (повторно)",
        ))
    else:
        return UndoOutcome("Цей тип дії поки не повторюється", kind="error")

    entry.undone_at = None
    log_action(
        db, order=order, operator=user, action_type="redo",
        field=entry.field, note=f"повторено: {entry.note}",
    )
    db.commit()

    if sync_error:
        return UndoOutcome(
            "Повторено в системі, але таблиця не оновилась: " + sync_error,
            kind="error", refresh_queue=True,
        )
    return UndoOutcome("Дію повторено", refresh_queue=True)
