"""Піч без зв'язку під час спікання: смуга на всіх екранах, «Зрозуміло» і
власний перемикач оператора (власник 20.11.26; логіка —
`app/services/furnace_power.py`). Звичайні `def` — threadpool, не event loop."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.routers.deps import get_current_user, get_db, templates
from app.services import feature_access, furnace_power

router = APIRouter()


def _slot(request: Request, db: Session, user) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_furnace_power.html",
        {"fp_notices": furnace_power.notices_for(db, user), "fp_now": datetime.now()},
    )


@router.get("/furnace-power/slot", response_class=HTMLResponse)
def get_furnace_power_slot(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return HTMLResponse("", status_code=401)
    return _slot(request, db, user)


@router.post("/furnace-power/ack", response_class=HTMLResponse)
def post_furnace_power_ack(request: Request, outage_id: int = Form(...), db: Session = Depends(get_db)):
    """«Зрозуміло» — знімає повідомлення для всіх (воно спільне)."""
    user = get_current_user(request, db)
    if user is None:
        return HTMLResponse("", status_code=401)
    if not feature_access.is_on(user, furnace_power.FEATURE):
        return HTMLResponse("", status_code=403)
    if not furnace_power.acknowledge(db, user, outage_id):
        return HTMLResponse("", status_code=404)
    return _slot(request, db, user)


# Кожне поле — СВОЄ слово (CLAUDE.md §14): відсутнє = «не чіпати».
_SWITCH = {"on": True, "off": False}


@router.post("/account/furnace-power", status_code=204)
def post_account_furnace_power(
    request: Request,
    alert: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return Response(status_code=401)
    if alert is None:
        return Response(status_code=204)
    if alert not in _SWITCH:
        return Response(status_code=422)
    if not feature_access.own_switch_allowed(user, furnace_power.FEATURE, _SWITCH[alert]):
        return Response(status_code=403)
    user.furnace_power_alert = _SWITCH[alert]
    db.commit()
    return Response(status_code=204)
