"""Оператори: створення, літера в черзі, вимкнення, скидання пароля."""

from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.auth import hash_password
from app.models import User
from app.routers.deps import get_current_user, login_redirect, get_db, is_loopback_request
from app.services.operators import (
    normalize_initial,
    validate_initial,
    validate_password,
    validate_role,
)

router = APIRouter()


@router.post("/settings/users", response_class=HTMLResponse)
async def create_operator(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()
    full_name = form.get("full_name", "").strip() or None
    role = form.get("role", "оператор").strip() or "оператор"
    initial = normalize_initial(form.get("sheet_initial", ""))

    if not username or not password:
        return RedirectResponse("/settings?error=логін+і+пароль+обов'язкові", status_code=303)

    # Спільні правила з `/setup` і «Кабінету» — app/services/operators.py.
    # Раніше тут не було ЖОДНОЇ перевірки: проходив пароль «1» і роль-одруківка.
    password_error = validate_password(password)
    if password_error:
        return RedirectResponse(f"/settings?error={quote(password_error)}", status_code=303)
    role_error = validate_role(role)
    if role_error:
        return RedirectResponse(f"/settings?error={quote(role_error)}", status_code=303)

    existing = db.scalar(select(User).where(User.username == username))
    if existing is not None:
        return RedirectResponse("/settings?error=такий+логін+вже+існує", status_code=303)

    if initial is not None:
        err = validate_initial(db, initial, exclude_user_id=None)
        if err:
            return RedirectResponse(f"/settings?error={quote(err)}", status_code=303)

    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name,
            role=role,
            sheet_initial=initial,
        )
    )
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/users/{user_id}/initial", response_class=HTMLResponse)
async def set_operator_initial(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Assign/change/clear an operator's sheet letter (admin only). Empty clears
    it (that operator's Sum3D writes then leave "Прорахував" untouched)."""
    admin = get_current_user(request, db)
    if admin is None:
        return login_redirect(request)
    if admin.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")

    form = await request.form()
    initial = normalize_initial(form.get("sheet_initial", ""))
    if initial is not None:
        err = validate_initial(db, initial, exclude_user_id=user_id)
        if err:
            return RedirectResponse(f"/settings?error={quote(err)}", status_code=303)

    target.sheet_initial = initial
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/users/{user_id}/toggle-active", response_class=HTMLResponse)
async def toggle_operator_active(
    request: Request, user_id: int, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")
    if target.id == user.id:
        return RedirectResponse(
            "/settings?error=не+можна+деактивувати+власний+обліковий+запис", status_code=303
        )

    target.is_active = not target.is_active
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/users/{user_id}/reset-password", response_class=HTMLResponse)
async def reset_operator_password(
    request: Request, user_id: int, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")

    form = await request.form()
    new_password = form.get("new_password", "").strip()
    if not new_password:
        return RedirectResponse("/settings?error=введіть+новий+пароль", status_code=303)
    password_error = validate_password(new_password)
    if password_error:
        return RedirectResponse(f"/settings?error={quote(password_error)}", status_code=303)

    target.password_hash = hash_password(new_password)
    # Скидання пароля мусить вибити оператора з усіх відкритих сесій — інакше
    # «змінили пароль» не означає «доступ закрито».
    target.session_epoch = int(getattr(target, "session_epoch", 0) or 0) + 1
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)
