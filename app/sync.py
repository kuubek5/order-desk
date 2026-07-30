from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, StatusEvent
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


def sync_tab(session: Session, sheet_tab: str, rows: list[OrderRow]) -> SyncResult:
    result = SyncResult()
    for row in rows:
        # Matched by position within the tab's data rows, not job_code, since
        # job_code is only filled in by the operator after the job is taken.
        existing = session.execute(
            select(Order).where(Order.sheet_tab == sheet_tab, Order.row_number == row.row_number)
        ).scalar_one_or_none()

        status = _infer_status(row)

        if existing is None:
            order = Order(source="lab", sheet_tab=sheet_tab, row_number=row.row_number, status=status, **_fields(row))
            session.add(order)
            session.flush()
            session.add(StatusEvent(order_id=order.id, status=status, actor="sync"))
            result.created += 1
            continue

        changed = False
        for field, value in _fields(row).items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True

        if existing.status != status:
            existing.status = status
            session.add(StatusEvent(order_id=existing.id, status=status, actor="sync"))
            changed = True

        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    return result
