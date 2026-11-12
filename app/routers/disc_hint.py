"""Рядок «який диск взяти» в картці листа (власник 12.11.26, варіант A).

Окремий запит, а не частина картки: читання теки дисків і файлів `.blk` не
має гальмувати відкриття листа. Показується лише тим, кому підказку дав
адміністратор (`disc_suggest.enabled_for`); решті — порожньо. Лише читання.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.models import EmailMessage
from app.routers.deps import get_current_user, get_db, templates
from app.services import disc_suggest
from app.settings_store import get_setting

router = APIRouter()


@router.get("/mail/{email_id}/disc-hint", response_class=HTMLResponse)
def get_disc_hint(request: Request, email_id: int, material_color: str = "",
                  db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not disc_suggest.enabled_for(db, user):
        return HTMLResponse("")
    email = db.get(EmailMessage, email_id)
    if email is None:
        raise HTTPException(status_code=404, detail="лист не знайдено")
    hint = disc_suggest.suggest(material_color, list(email.attachments or []),
                                get_setting(db, "cam_blanks_path"))
    return templates.TemplateResponse(request, "_mail_disc_hint.html", {"hint": hint})
