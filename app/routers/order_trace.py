"""Екран «Де ця робота?» — HTTP-бік.

Доменна частина (де шукати й як пояснювати) — `app/services/order_trace.py`.

Лише адмін, як і решта діагностики: відповіді тут — про внутрішній устрій
(вкладки, знімки, синхронізація), і дії за ними теж адмінські. Оператору для
«де моя робота» є звичайний пошук у черзі.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_current_user, get_db, login_redirect, templates
from app.services import order_trace
from app.services.order_dates import order_date


router = APIRouter()


@router.get("/trace", response_class=HTMLResponse)
def get_trace(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if getattr(user, "role", None) != "адмін":
        raise HTTPException(status_code=404, detail="not found")

    result = order_trace.trace(db, q) if q.strip() else None
    return templates.TemplateResponse(
        request,
        "order_trace.html",
        {
            "request": request,
            "user": user,
            "topbar_active": "trace",
            "q": q,
            "result": result,
            # Дата роботи рахується сервісом робочої доби, а не шаблоном:
            # `date.today()` у Jinja дав би інший день уночі (§14).
            "order_date": order_date,
        },
    )
