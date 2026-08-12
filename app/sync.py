from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Comment, Order, ReworkRecord, StatusEvent
from app.parser import OrderRow


def _infer_status(row: OrderRow) -> str:
    if row.milled:
        return "відфрезеровано"
    if row.calculated:
        return "прораховано"
    if row.sum3d_id:
        return "прийнято"
    return "нове"


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0


def _fields(row: OrderRow) -> dict:
    return {
        "work_order_no": row.work_order_no or None,
        "job_code": row.job_code or None,
        "quantity": row.quantity or None,
        "material_color": row.material_color or None,
        "kind": row.kind or None,
        "due_time": row.due_time,
        "technician_name": row.technician_name or None,
        "cam_comment": row.cam_comment or None,
        "sum3d_id": row.sum3d_id or None,
        "calculated_raw": row.calculated or None,
        "milled_raw": row.milled or None,
        "last_milled_date": row.last_milled_date or None,
        "mill_count": row.mill_count or None,
    }


def _rework_from_row(row: OrderRow) -> dict | None:
    """Rework/БРАК fields for a sheet row, or None when the row records no
    rework. Technicians fill the blame columns (обладнання/технік/адміністратор/
    клієнт → unit count) and the cam operator fills the redo comment / ID /
    прорахував / відфрезерував columns — the presence of any of those marks a
    rework. `occurrence` is the sheet's "який раз фрезерується" count."""
    blame_labels = list(row.rework_blame.keys())
    blame_quantities = [q for q in row.rework_blame.values() if q]
    if not (
        blame_labels
        or row.redo_quantity
        or row.redo_cam_comment
        or row.redo_sum3d_id
        or row.redo_calculated
        or row.redo_milled
    ):
        return None

    occurrence = None
    if row.mill_count and row.mill_count.strip().isdigit():
        occurrence = int(row.mill_count.strip())

    return {
        "occurrence": occurrence,
        "blame": ", ".join(blame_labels) or None,
        "blame_quantity": ", ".join(blame_quantities) or None,
        "redo_quantity": row.redo_quantity or None,
        "cam_comment": row.redo_cam_comment or None,
        "sum3d_id": row.redo_sum3d_id or None,
        "calculated_raw": row.redo_calculated or None,
        "milled_raw": row.redo_milled or None,
    }


def _sync_rework(session: Session, order_id: int, rework: dict | None) -> bool:
    """Upsert the single sheet-sourced ReworkRecord for an order. Reworks come
    only from the sheet today, so at most one record per order is kept and
    matched by order_id — idempotent across repeated syncs. Never deletes: a
    cleared sheet leaves the last recorded rework intact. Returns True if it
    created or changed anything."""
    if rework is None:
        return False

    existing = session.execute(
        select(ReworkRecord).where(ReworkRecord.order_id == order_id)
    ).scalars().first()

    if existing is None:
        session.add(ReworkRecord(order_id=order_id, **rework))
        return True

    changed = False
    for field, value in rework.items():
        if getattr(existing, field) != value:
            setattr(existing, field, value)
            changed = True
    return changed


def _new_sheet_comment(previous: str | None, current: str | None) -> str | None:
    """Return only newly appended sheet text when possible, else a snapshot."""
    previous = (previous or "").strip()
    current = (current or "").strip()
    if not current or current == previous:
        return None
    if previous and current.startswith(previous):
        appended = current[len(previous):].strip()
        return appended or None
    return current


def _should_apply_sheet_status(current: str, inferred: str) -> bool:
    """Apply only forward sheet progress and preserve portal-only states."""
    if current in {"проблема", "переробка", "знайдено при видачі", "видано"}:
        return False

    progress = {
        "нове": 0,
        "прийнято": 1,
        "прораховано": 2,
        "у фрезеруванні": 3,
        "відфрезеровано": 4,
    }
    current_rank = progress.get(current)
    inferred_rank = progress.get(inferred)
    if current_rank is None or inferred_rank is None:
        return current != inferred
    return inferred_rank > current_rank


def sync_tab(session: Session, sheet_tab: str, rows: list[OrderRow]) -> SyncResult:
    result = SyncResult()
    for row in rows:
        # Matched by position within the tab's data rows, not job_code, since
        # job_code is only filled in by the operator after the job is taken.
        existing = session.execute(
            select(Order).where(Order.sheet_tab == sheet_tab, Order.row_number == row.row_number)
        ).scalar_one_or_none()

        status = _infer_status(row)

        rework = _rework_from_row(row)

        if existing is None:
            order = Order(source="lab", sheet_tab=sheet_tab, row_number=row.row_number, status=status, **_fields(row))
            session.add(order)
            session.flush()
            session.add(StatusEvent(order_id=order.id, status=status, actor="sync"))
            if row.cam_comment:
                session.add(Comment(order_id=order.id, source="sheet", text=row.cam_comment))
            _sync_rework(session, order.id, rework)
            result.created += 1
            continue

        changed = False
        sheet_comment = _new_sheet_comment(existing.cam_comment, row.cam_comment)
        for field, value in _fields(row).items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True

        if sheet_comment:
            session.add(Comment(order_id=existing.id, source="sheet", text=sheet_comment))

        # The sheet can only represent progress through milling. Portal-only
        # handout states must survive the next read from the sheet.
        if _should_apply_sheet_status(existing.status, status):
            existing.status = status
            session.add(StatusEvent(order_id=existing.id, status=status, actor="sync"))
            changed = True

        if _sync_rework(session, existing.id, rework):
            changed = True

        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    return result
