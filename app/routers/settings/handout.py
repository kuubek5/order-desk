"""Правила екрана видачі: QC-чеклист перед «знайдено» і зведення листів
Конвеєра в один рядок (той рядок і є рядком видачі)."""

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_db
from app.services.handout_qc import set_qc_checklist
from app.services.mail_merge import set_merge_enabled

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


@router.post("/settings/mail-merge-rows")
async def save_mail_merge_rows(request: Request, db: Session = Depends(get_db)):
    """Увімкнути/вимкнути зведення листів Конвеєра в один рядок (власник
    05.10.26): кілька листів одного клієнта й кольору, що лягли в одну теку з
    тим самим Sum3D, — одна робота з сумою кількості, один рядок у таблиці й
    на видачі.

    Окремий роут і окрема форма, а не друге поле у формі QC: незазначена
    галочка не приходить узагалі, тож спільна форма вимикала б сусідній
    перемикач щоразу, коли зберігають цей (§14 «Порожнє поле»). Адмінська, як
    QC: міняє ПРОЦЕС прийняття для всіх.
    """
    require_settings_admin(request, db)
    form = await request.form()
    enabled = bool(form.get("mail_merge_rows"))
    set_merge_enabled(db, enabled)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": (
            "Зведення увімкнено: листи одного клієнта й кольору з однієї теки "
            "Конвеєр прийматиме одним рядком із сумою кількості."
            if enabled
            else "Зведення вимкнено: кожен лист Конвеєра — окремий рядок, як раніше."
        ),
    }
    return RedirectResponse("/settings?saved=1#handout", status_code=303)

