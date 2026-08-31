"""Екран «Верстати» — живі кадри екранів RemiCORE, тільки перегляд.

Фаза 1: кадр раз на кілька секунд, без розпізнавання чисел. Це вже закриває
головний сценарій — «глянути, що на верстаті, не відкриваючи RustDesk».
Фаза 2 (OCR відсотка/часу/програми) додасться поверх цих самих кадрів.

Керування верстатом відсутнє свідомо й повністю: знімок іде через
app/furnace_vnc.py, який фізично не вміє слати ввід (перевірено стендом).
Доменна логіка — app/services/machines.py; тут лише HTTP.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_current_user, get_db, templates
from app.services.machines import (
    POLL_INTERVAL_SECONDS,
    poll_all,
    resolve_frame,
    snapshot,
)

router = APIRouter()


def _context(request: Request, db: Session, user) -> dict:
    return {
        "request": request,
        "user": user,
        "topbar_active": "machines",
        "cards": snapshot(db),
        "poll_seconds": int(POLL_INTERVAL_SECONDS),
    }


@router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "machines.html", _context(request, db, user))


@router.get("/machines/cards", response_class=HTMLResponse)
def machines_cards(request: Request, db: Session = Depends(get_db)):
    """Фрагмент для полла HTMX — повний свап безпечний: на екрані немає ні
    полів вводу, ні дій над рядком (та сама причина, що на пічках)."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(request, "_machine_cards.html", _context(request, db, user))


@router.post("/machines/refresh", response_class=HTMLResponse)
def machines_refresh(request: Request, db: Session = Depends(get_db)):
    """Зняти кадри просто зараз. Це ЧИТАННЯ, а не дія над верстатом."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    poll_all(db)
    return templates.TemplateResponse(request, "_machine_cards.html", _context(request, db, user))


@router.get("/machines/{key}/frame.png")
def machine_frame(request: Request, key: str, db: Session = Depends(get_db)):
    """Останній кадр екрана верстата.

    `key` НЕ підставляється у шлях: resolve_frame звіряє його зі станом
    процесу, і шлях будується з відомого верстата. Невідомий ключ — 404,
    а не спроба відкрити те, що написали в адресному рядку.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    path = resolve_frame(key)
    if path is None:
        raise HTTPException(status_code=404, detail="кадру ще немає")
    # Кадр перезаписується під тим самим іменем — без no-store браузер
    # показував би вчорашню картинку.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})
