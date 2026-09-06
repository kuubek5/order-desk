"""Правила екрана видачі. Поки одне: QC-чеклист перед «знайдено»."""

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_db
from app.services.handout_qc import set_qc_checklist

from .common import require_settings_admin

router = APIRouter()


@router.post("/settings/handout-qc")
async def save_handout_qc(request: Request, db: Session = Depends(get_db)):
    """Увімкнути/вимкнути три звірки перед відміткою «знайдено».

    Адмінська, а не операторська настройка: вона міняє ПРОЦЕС видачі для всіх,
    а не вигляд екрана під себе — тому не в `OPERATOR_EDITABLE_KEYS`.
    Незазначена галочка форми не приходить узагалі, звідси `bool(form.get(...))`.
    """
    require_settings_admin(request, db)
    form = await request.form()
    enabled = bool(form.get("handout_qc"))
    set_qc_checklist(db, enabled)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": (
            "QC-чеклист увімкнено: перед «знайдено» треба буде звірити три пункти."
            if enabled
            else "QC-чеклист вимкнено: «знайдено» знову в один клік."
        ),
    }
    return RedirectResponse("/settings?saved=1#handout", status_code=303)
