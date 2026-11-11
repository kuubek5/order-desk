"""Віджет «Світло»: фрагмент плашки й сітки годин для шапки CRM.

Один роут — власний хвилинний полл віджета. Дані вже лежать у памʼяті
(`power_schedule`, фоновий прохід раз на 10 хв), тож роут не ходить ні в базу
по налаштування, ні в мережу: полл раз на хвилину з кожної відкритої вкладки
не має коштувати нічого.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_current_user, get_db, templates

router = APIRouter()


@router.get("/power/widget", response_class=HTMLResponse)
def power_widget(request: Request, db: Session = Depends(get_db)):
    """Обгортка віддається завжди (порожня й схована — теж): у ній живе полл."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(request, "_power_widget.html", {})
