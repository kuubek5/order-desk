"""Екран «Що не так» — HTTP-бік.

Доменна частина (звідки беруться проблеми і як вони перекладаються людською
мовою) — `app/services/whats_wrong.py`. Тут лише вхід, вікно й шаблон.

Лише адмін. Не тому, що операторам шкідливо знати, а тому, що кожна дія на
цьому екрані — його: перейменувати вкладку в таблиці, поміняти пароль пошти,
піти до ПК верстата. Оператору для «щось не так» є кнопка «Написати нам».
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_current_user, get_db, login_redirect, templates
from app.services import whats_wrong


router = APIRouter()

# Вікна, які можна обрати. Доба — «що сталось, поки мене не було»; тиждень —
# «чи це вже повторюється»; година — «що коїться просто зараз».
WINDOWS = {1: "Година", 24: "Доба", 168: "Тиждень"}


@router.get("/wrong", response_class=HTMLResponse)
def get_whats_wrong(request: Request, hours: int = 24, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if getattr(user, "role", None) != "адмін":
        raise HTTPException(status_code=404, detail="not found")

    hours = hours if hours in WINDOWS else 24
    problems = whats_wrong.collect(db, hours=hours)
    return templates.TemplateResponse(
        request,
        "whats_wrong.html",
        {
            "request": request,
            "user": user,
            "topbar_active": "wrong",
            "hours": hours,
            "windows": WINDOWS,
            "problems": problems,
            "stop_level": whats_wrong.LEVEL_STOP,
            "problem_level": whats_wrong.LEVEL_PROBLEM,
        },
    )
