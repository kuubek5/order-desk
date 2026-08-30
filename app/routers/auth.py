"""Вхід у систему, ліцензія і кабінет оператора.

Без валідного входу не видно нічого (CLAUDE.md §9, екран 0), а без валідної
ліцензії — навіть сторінки входу: /license проходить повз ліцензійний
middleware, решта ні.
"""

from threading import Lock

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import hash_password, verify_password
from app.license import get_license_status, get_machine_id, verify_license_key
from app.models import User
from app.routers.deps import get_current_user, get_db, templates
from app.services.operators import (
    normalize_initial,
    user_count,
    validate_first_admin,
    validate_initial,
)
from app.settings_store import set_setting

router = APIRouter()

# The desktop build runs one application process. The lock keeps two local
# first-run submissions from both passing the empty-database check.
FIRST_ADMIN_LOCK = Lock()


@router.get("/license", response_class=HTMLResponse)
def license_form(request: Request, db: Session = Depends(get_db)):
    status = get_license_status(db)
    return templates.TemplateResponse(
        request, "license.html", {"status": status, "machine_id": get_machine_id()}
    )


@router.post("/license", response_class=HTMLResponse)
async def license_submit(
    request: Request, license_key: str = Form(""), db: Session = Depends(get_db)
):
    machine_id = get_machine_id()
    status = verify_license_key(license_key, machine_id)
    if not status.valid:
        return templates.TemplateResponse(
            request,
            "license.html",
            {
                "status": status,
                "machine_id": machine_id,
                "error": status.reason,
                "license_key_input": license_key.strip(),
            },
            status_code=400,
        )

    set_setting(db, "license_key", license_key.strip())
    db.commit()

    destination = "/setup" if user_count(db) == 0 else "/"
    return RedirectResponse(destination, status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db)):
    if user_count(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    if get_current_user(request, db) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Session = Depends(get_db)):
    if user_count(db) != 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {})


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(""),
    full_name: str = Form(""),
    password: str = Form(""),
    password_confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    if user_count(db) != 0:
        return RedirectResponse("/login", status_code=303)

    values, error = validate_first_admin(
        username, full_name, password, password_confirmation
    )
    if error is not None:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": error, "username": username.strip(), "full_name": full_name.strip()},
            status_code=400,
        )

    with FIRST_ADMIN_LOCK:
        if user_count(db) != 0:
            return RedirectResponse("/login", status_code=303)
        assert values is not None
        user = User(
            username=values["username"],
            full_name=values["full_name"],
            password_hash=hash_password(values["password"]),
            role="адмін",
            is_active=True,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return RedirectResponse("/login", status_code=303)
        db.refresh(user)

    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/settings?welcome=1", status_code=303)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    if user_count(db) == 0:
        return RedirectResponse("/setup", status_code=303)
    user = db.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Невірний логін або пароль"}
        )
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account", response_class=HTMLResponse)
async def get_account(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse(request, "account.html", {"user": user})


UI_THEMES = {"", "forge"}
UI_ICON_STYLES = {"", "thin", "duo", "fill", "bold", "neon"}


@router.post("/account/appearance")
async def post_account_appearance(
    request: Request,
    theme: str = Form(""),
    icons: str = Form(""),
    db: Session = Depends(get_db),
):
    """Зберегти тему/стиль іконок оператора. Викликається fetch'ем з кабінету
    після миттєвого застосування на клієнті — тому відповідь 204, без
    перерендеру сторінки. Невалідні значення відсікаються, а не «якось
    зберігаються»: атрибут з форми летить прямо в <html> кожної сторінки."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if theme not in UI_THEMES or icons not in UI_ICON_STYLES:
        return Response(status_code=422)
    user.ui_theme = theme
    user.ui_icon_style = icons
    db.commit()
    return Response(status_code=204)


@router.post("/account/password", response_class=HTMLResponse)
async def post_account_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    error = None
    if not verify_password(current_password, user.password_hash):
        error = "Поточний пароль невірний"
    elif len(new_password) < 6:
        error = "Новий пароль має бути не коротшим за 6 символів"
    elif new_password != confirm_password:
        error = "Паролі не збігаються"

    if error:
        return templates.TemplateResponse(request, "account.html", {"user": user, "error": error})

    user.password_hash = hash_password(new_password)
    db.commit()

    return templates.TemplateResponse(request, "account.html", {"user": user, "saved": "Пароль змінено"})


@router.post("/account/initial", response_class=HTMLResponse)
async def post_account_initial(
    request: Request,
    sheet_initial: str = Form(""),
    db: Session = Depends(get_db),
):
    """Operator sets their OWN sheet letter (the one stamped into «Прорахував»
    when they enter a Sum3D). Self-service counterpart of the admin route
    /settings/users/{id}/initial — same normalize + uniqueness validation."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    initial = normalize_initial(sheet_initial)
    if initial is not None:
        err = validate_initial(db, initial, exclude_user_id=user.id)
        if err:
            return templates.TemplateResponse(request, "account.html", {"user": user, "error": err})

    user.sheet_initial = initial
    db.commit()
    return templates.TemplateResponse(request, "account.html", {"user": user, "saved": "Літеру збережено"})
