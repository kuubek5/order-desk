"""Сповіщення: стан для клієнта і збереження налаштувань під оператора.

Це налаштування ОПЕРАТОРА, не машини, — тому обидва роути свідомо без
адмінського гейта (виняток зафіксовано в tests/test_admin_gate.py).
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.models import EmailMessage, Order
from app.routers.deps import get_current_user, get_db, toast_response
from app.services.shift import open_note_count as open_shift_note_count
from app.settings_store import set_notify_prefs
from app.sync_heartbeat import sync_status_pair
from app.update_check import get_known_update

router = APIRouter()


@router.get("/api/notify-state")
def api_notify_state(request: Request, db: Session = Depends(get_db)):
    """Cheap snapshot the client polls to detect system events worth a popup.

    Deliberately NOT a push channel: the browser compares this against its own
    previous snapshot and raises a toast on a TRANSITION (ok → error, count
    grew). That keeps the trigger logic in one place client-side and means a
    missed poll can never replay an old alert — the next poll just reflects
    reality. Everything here is already computed for the queue page, so this
    costs two scalar counts and two in-memory heartbeat reads.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    status = sync_status_pair(db, datetime.now())
    release = get_known_update()
    return {
        "sheet": status["sheet"]["state"],
        "mail": status["mail"]["state"],
        "sheet_label": status["sheet"]["label"],
        "mail_label": status["mail"]["label"],
        "orders": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status != "видано", Order.archived_at.is_(None))
        ) or 0,
        "mail_pending": db.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(EmailMessage.status == "нове", EmailMessage.filter_category.is_(None))
        ) or 0,
        # Works a technician corrected in the sheet and nobody has acknowledged
        # yet. A rise means a fresh correction — the client toasts on that, so
        # the operator learns about it even while looking at the machines.
        "changed": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.sheet_changed_at.is_not(None), Order.archived_at.is_(None))
        ) or 0,
        # Відкриті записки передачі зміни — приріст означає, що колега
        # щойно щось передав. Той самий предикат, що й дошка/бейдж
        # (app/services/shift.py), щоб три місця не розходились.
        "shift": open_shift_note_count(db),
        "update": release.version if release else None,
    }


@router.post("/settings/notifications")
async def save_notification_prefs(request: Request, db: Session = Depends(get_db)):
    """Save popup look, placement and which system triggers may fire one.

    Operator-facing (not admin-only): these are per-installation display
    preferences on a single-workstation app, not a security boundary — the same
    reasoning that makes the folder paths operator-editable.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    form = await request.form()
    set_notify_prefs(
        db,
        style=(form.get("notify_style") or "").strip(),
        position=(form.get("notify_position") or "").strip(),
        events=form.getlist("notify_events"),
    )
    db.commit()
    if request.headers.get("HX-Request") == "true":
        return toast_response("Налаштування сповіщень збережено")
    # Без JS повертаємо туди, ЗВІДКИ прийшли: розділ переїхав у кабінет
    # (/account, вкладка «Сповіщення»), і старий редірект на /settings кидав
    # би людину на «Стан системи» — секції з таким якорем там більше немає.
    return RedirectResponse("/account#notifications", status_code=303)
