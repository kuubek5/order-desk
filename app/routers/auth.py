"""Вхід у систему, ліцензія і кабінет оператора.

Без валідного входу не видно нічого (CLAUDE.md §9, екран 0), а без валідної
ліцензії — навіть сторінки входу: /license проходить повз ліцензійний
middleware, решта ні.
"""

import logging
from threading import Lock

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth import hash_password, verify_password
from app.license import (
    REASON_NOT_ACTIVATED,
    get_license_status,
    get_machine_id,
    verify_license_key,
)
from app.models import User
from app.services.widget_order import clean_side_order, clean_strip_order
from app.routers.deps import UI_SESSION_KEY, get_current_user, login_redirect, get_db, templates
from app.services.look_prefs import (
    LookError,
    apply_handout_look,
    apply_mail_look,
    apply_queue_look,
)
from app.services.operators import (
    normalize_initial,
    user_count,
    validate_first_admin,
    validate_initial,
)
from app.settings_store import set_setting

logger = logging.getLogger(__name__)

router = APIRouter()

# The desktop build runs one application process. The lock keeps two local
# first-run submissions from both passing the empty-database check.
FIRST_ADMIN_LOCK = Lock()


@router.get("/license", response_class=HTMLResponse)
def license_form(request: Request, db: Session = Depends(get_db)):
    status = get_license_status(db)
    # Справжню проблему (сплив терміну, ключ видано іншій машині, збій ключа
    # шифрування) показуємо червоним, щоб оператор одразу зрозумів причину.
    # Чисте «ще не активовано» лишаємо нейтральним підзаголовком.
    error = (
        status.reason
        if not status.valid and status.reason != REASON_NOT_ACTIVATED
        else None
    )
    return templates.TemplateResponse(
        request,
        "license.html",
        {"status": status, "machine_id": get_machine_id(), "error": error},
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
        # Логін повертаємо в поле: стирати його після одруківки в ПАРОЛІ
        # означає змушувати набирати обидва заново щозміни. Setup цю саму
        # ввічливість уже має.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Невірний логін або пароль", "username": username.strip()},
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
        return login_redirect(request)

    return templates.TemplateResponse(request, "account.html", {"user": user})


# "" лишається дозволеним лише для сумісності зі старими БД (до зміни дефолту
# на forge). Нові збереження шлють "teal" або "forge"; "" резолвиться в forge.
UI_THEMES = {"", "teal", "forge"}
UI_ICON_STYLES = {"", "thin", "duo", "fill", "bold", "neon"}
UI_BUTTON_STYLES = {"", "outline", "glass", "dashed", "solid"}
UI_LOADER_STYLES = {"", "beacon", "sweep", "orbit", "ring"}
UI_CHIP_STYLES = {"", "solid", "dashed", "marker", "gradient"}
# Віджет верстатів: "" = «Пил на сталі» / «Сегменти» — дефолт, обраний
# власником 03.09.26; решта з тієї ж галереї; none/off — вимкнути.
UI_MACHINE_ARTS = {"", "burr", "flower", "titanium", "toolpath", "none"}
UI_MACHINE_STRIPS = {"", "edge", "fill", "ring", "ticker", "off"}
# Картки на екрані «Верстати»: "" = «Портрет» (дефолт), "frame" = живий кадр у плитці.
UI_MACHINE_CARDS = {"", "frame"}


@router.post("/account/appearance")
async def post_account_appearance(
    request: Request,
    theme: str = Form(""),
    icons: str = Form(""),
    buttons: str = Form(""),
    loader: str = Form(""),
    chips: str = Form(""),
    machine_art: str = Form(""),
    machine_strip: str = Form(""),
    machine_card: str = Form(""),
    db: Session = Depends(get_db),
):
    """Зберегти візуальний набір оператора. Викликається fetch'ем з кабінету
    після миттєвого застосування на клієнті — тому відповідь 204, без
    перерендеру сторінки. Невалідні значення відсікаються, а не «якось
    зберігаються»: атрибути з форми летять прямо в <html> кожної сторінки."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if (
        theme not in UI_THEMES
        or icons not in UI_ICON_STYLES
        or buttons not in UI_BUTTON_STYLES
        or loader not in UI_LOADER_STYLES
        or chips not in UI_CHIP_STYLES
        or machine_art not in UI_MACHINE_ARTS
        or machine_strip not in UI_MACHINE_STRIPS
        or machine_card not in UI_MACHINE_CARDS
    ):
        return Response(status_code=422)
    user.ui_theme = theme
    user.ui_icon_style = icons
    user.ui_button_style = buttons
    user.ui_loader_style = loader
    user.ui_chip_style = chips
    user.ui_machine_art = machine_art
    user.ui_machine_strip = machine_strip
    user.ui_machine_card = machine_card
    db.commit()
    return Response(status_code=204)


@router.post("/account/layout", status_code=204)
async def post_account_layout(
    request: Request,
    scope: str = Form(...),
    order: str = Form(""),
    db: Session = Depends(get_db),
):
    """Порядок віджетів черги після перетягування (режим редагування в
    шестерні вигляду). `scope`: "side" — секції правої панелі, "strip" —
    смуга верстатів. Чуже в списку відсіюється, а не відхиляється: секція
    могла зникнути з розмітки, верстат — з переліку, і 422 на це означало б,
    що оператор більше нічого не може перетягнути."""
    user = get_current_user(request, db)
    if user is None:
        return Response(status_code=401)
    if scope == "side":
        user.queue_side_order = clean_side_order(order)
    elif scope == "strip":
        user.queue_strip_order = clean_strip_order(order)
    else:
        return Response(status_code=422)
    db.commit()
    return Response(status_code=204)


@router.post("/account/look", status_code=204)
async def post_account_look(
    request: Request,
    scope: str = Form(...),
    row_pad: int = Form(0),
    list_width: int = Form(0),
    density: str = Form(""),
    mat_style: str = Form(""),
    step: int = Form(0),
    layout: str = Form(""),
    db: Session = Depends(get_db),
):
    """Зберегти вигляд списку (шестерня) — один роут на обидва екрани.

    Викликається fetch'ем ПІСЛЯ того, як зміну вже застосовано на клієнті:
    тому 204 і жодного перерендеру — затиснута «+» не має чекати на мережу.
    Числа підтягуються до меж, бо приходять від кнопок самого оператора;
    пресети й крок — з фіксованих переліків, бо приходять з розмітки, і
    несподіване значення там означає помилку, яку краще побачити.
    """
    user = get_current_user(request, db)
    if user is None:
        return Response(status_code=401)
    try:
        if scope == "mail":
            apply_mail_look(user, row_pad=row_pad, list_width=list_width, step=step)
        elif scope == "queue":
            apply_queue_look(
                user, density=density, row_pad=row_pad, mat_style=mat_style, step=step
            )
        elif scope == "handout":
            apply_handout_look(user, layout=layout)
        else:
            return Response(status_code=422)
    except LookError:
        return Response(status_code=422)
    db.commit()
    # Дзеркало в сесії мусить оновитись разом із БД, інакше сторінка помилки
    # показала б попередній вигляд (див. ui_prefs).
    try:
        request.session.pop(UI_SESSION_KEY, None)
    except Exception:  # noqa: BLE001 — налаштування не варте зламаного запиту
        logger.debug("не вдалось скинути дзеркало ui в сесії", exc_info=True)
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
        return login_redirect(request)

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
        return login_redirect(request)

    initial = normalize_initial(sheet_initial)
    if initial is not None:
        err = validate_initial(db, initial, exclude_user_id=user.id)
        if err:
            return templates.TemplateResponse(request, "account.html", {"user": user, "error": err})

    user.sheet_initial = initial
    db.commit()
    return templates.TemplateResponse(request, "account.html", {"user": user, "saved": "Літеру збережено"})
