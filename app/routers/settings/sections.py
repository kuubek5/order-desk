"""Доступ до розділів: блокування сторінок і PIN «Виробітку»."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.services.section_gate import AUDIENCE_ALL, set_section_audience, set_section_state
from app.routers.deps import get_db
from app.settings_store import set_setting
from .common import require_settings_admin

router = APIRouter()


@router.post("/settings/vyrobitok-pin")
async def save_vyrobitok_pin(request: Request, db: Session = Depends(get_db)):
    """ПІН розділу «Виробіток». Порожнє значення = зняти захист (розділ
    відкритий). Значення шифрується, як решта налаштувань; на екрані показуємо
    лише ознаку «задано», не сам код."""
    require_settings_admin(request, db)
    form = await request.form()
    pin = (form.get("vyrobitok_pin") or "").strip()
    set_setting(db, "vyrobitok_pin", pin)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": "ПІН «Виробітку» знято." if not pin else "ПІН «Виробітку» збережено.",
    }
    return RedirectResponse("/settings?saved=1#operators", status_code=303)


@router.post("/settings/sections/{section}")
async def save_section_state(section: str, request: Request, db: Session = Depends(get_db)):
    """Стан розділу для гейта «в розробці / тестується» (банер адміна над
    розділом): назва арту-блокатора або "open". Повертає туди, звідки
    натиснули — банер передає свій шлях у `back`."""
    require_settings_admin(request, db)
    form = await request.form()
    # Кнопка «Відкрити для всіх» шле state=open; зміна арту в select — variant.
    # Кнопка має старшинство: якщо натиснули її, select теж приїде, але не він
    # є наміром.
    state = (form.get("state") or form.get("variant") or "").strip()
    try:
        set_section_state(db, section, state)
        # Аудиторія: галочка «усі ролі» має старшинство; інакше — відмічені ролі.
        # Поле приходить лише з картки Налаштувань; банер його не шле, тому там
        # аудиторія лишається як була.
        if form.get("audience_all"):
            set_section_audience(db, section, AUDIENCE_ALL)
        elif "role" in form:
            set_section_audience(db, section, form.getlist("role"))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="невідомий розділ або стан")
    db.commit()
    back = (form.get("back") or "/").strip()
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    return RedirectResponse(back, status_code=303)
