"""Екран «Що не так» — HTTP-бік.

Доменна частина (звідки беруться проблеми і як вони перекладаються людською
мовою) — `app/services/whats_wrong.py`. Тут лише вхід, вікно й шаблон.

Лише адмін. Не тому, що операторам шкідливо знати, а тому, що кожна дія на
цьому екрані — його: перейменувати вкладку в таблиці, поміняти пароль пошти,
піти до ПК верстата. Оператору для «щось не так» є кнопка «Написати нам».
"""

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
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
            # Заглушені — окремою смугою, а не сховані: людина мусить бачити,
            # що саме мовчить і до якого числа.
            "muted": whats_wrong.muted_problems(db, hours=hours),
            "mutes": whats_wrong.active_mutes(db),
            "mute_max_days": whats_wrong.MUTE_MAX_DAYS,
            "stop_level": whats_wrong.LEVEL_STOP,
            "problem_level": whats_wrong.LEVEL_PROBLEM,
        },
    )


def _back(hours: int) -> RedirectResponse:
    return RedirectResponse(f"/wrong?hours={hours}", status_code=303)


@router.post("/wrong/mute")
def post_mute(
    request: Request,
    key: str = Form(...),
    title: str = Form(""),
    note: str = Form(""),
    days: int = Form(7),
    hours: int = Form(24),
    db: Session = Depends(get_db),
):
    """Заглушити відому причину на строк.

    Глушиться пара «пристрій + причина» (`Problem.key`), не пристрій: інша
    причина на тому ж верстаті пройде й засвітиться. Строк обмежений
    (`MUTE_MAX_DAYS`) — вічний глушник робить екран тривог сліпим, просто тихо.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if getattr(user, "role", None) != "адмін":
        raise HTTPException(status_code=404, detail="not found")

    key = (key or "").strip()
    if key:
        whats_wrong.mute(
            db, key=key, title=title, days=days, note=note, user_id=user.id
        )
        db.commit()
    return _back(hours if hours in WINDOWS else 24)


@router.post("/wrong/unmute")
def post_unmute(
    request: Request,
    key: str = Form(...),
    hours: int = Form(24),
    db: Session = Depends(get_db),
):
    """Повернути причину в перелік достроково."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if getattr(user, "role", None) != "адмін":
        raise HTTPException(status_code=404, detail="not found")

    if whats_wrong.unmute(db, key=(key or "").strip()):
        db.commit()
    return _back(hours if hours in WINDOWS else 24)
