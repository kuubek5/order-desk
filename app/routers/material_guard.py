"""Сторож матеріалу: слот червоних карток у черзі, «Погоджено», перемикачі
оператора (власник 18.11.26, MATERIAL_GUARD_BRIEF.md; логіка —
`app/services/material_guard.py`). Звичайні `def` — threadpool, не event loop."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.routers.deps import get_current_user, get_db, templates
from app.services import material_guard

router = APIRouter()


def _slot(request: Request, db: Session, user) -> HTMLResponse:
    alerts = material_guard.alerts_for(db, user)
    view = "card" if (getattr(user, "material_guard_view", "") or "") == "card" else "banner"
    return templates.TemplateResponse(
        request, "_material_guard.html", {"mg_alerts": alerts, "mg_view": view}
    )


@router.get("/material-guard/slot", response_class=HTMLResponse)
def get_material_guard_slot(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return HTMLResponse("", status_code=401)
    return _slot(request, db, user)


@router.post("/material-guard/ack", response_class=HTMLResponse)
def post_material_guard_ack(
    request: Request,
    layer: str = Form(...),
    order_id: Optional[int] = Form(None),
    disc_key: str = Form(""),
    scope: str = Form("order"),
    db: Session = Depends(get_db),
):
    """«Погоджено» — для роботи (`scope=order`) або для всього диска
    (`scope=disc`, невірно названий диск). Відповідь — оновлений слот."""
    user = get_current_user(request, db)
    if user is None:
        return HTMLResponse("", status_code=401)
    if material_guard.access_of(user) == material_guard.ACCESS_DENY:
        return HTMLResponse("", status_code=403)
    if scope not in ("order", "disc"):
        return HTMLResponse("", status_code=422)
    try:
        material_guard.acknowledge(db, user, layer=layer, order_id=order_id, disc_key=disc_key,
                                   whole_disc=scope == "disc")
    except ValueError:
        return HTMLResponse("", status_code=422)
    return _slot(request, db, user)


# Кожне поле — СВОЄ слово (CLAUDE.md §14 «Порожнє поле = поля не було»): кнопка
# шле лише своє поле, відсутнє = «не чіпати».
_SWITCH = {"on": True, "off": False}
_VIEW = {"banner": "", "card": "card"}


@router.post("/account/material-guard", status_code=204)
def post_account_material_guard(
    request: Request,
    guard: Optional[str] = Form(None),
    letter: Optional[str] = Form(None),
    view: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return Response(status_code=401)
    if (guard is not None and guard not in _SWITCH) or (letter is not None and letter not in _SWITCH) \
            or (view is not None and view not in _VIEW):
        return Response(status_code=422)
    # Доступ від адміна (20.11.26): «заборонено» — перемикачі не діють;
    # «завжди увімкнено» — основну перевірку оператор не вимкне. Вигляд — завжди.
    access = material_guard.access_of(user)
    if access == material_guard.ACCESS_DENY and (guard is not None or letter is not None):
        return Response(status_code=403)
    if access == material_guard.ACCESS_FORCE and guard == "off":
        return Response(status_code=403)
    if guard is not None:
        user.material_guard = _SWITCH[guard]
    if letter is not None:
        user.material_guard_letter = _SWITCH[letter]
    if view is not None:
        user.material_guard_view = _VIEW[view]
    db.commit()
    return Response(status_code=204)
