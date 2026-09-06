"""Обладнання: печі, верстати, портрети верстатів.

ПОРЯДОК ОГОЛОШЕННЯ ВАЖИТЬ. FastAPI приміряє маршрути згори вниз, тому
`/settings/furnaces/password` і `/settings/furnaces/background` мусять стояти
ВИЩЕ `/settings/furnaces/{furnace_id}`: інакше «password» їде в роут як номер
пічки і пароль не зберігається (спіймано живою перевіркою, сторожі —
tests/test_furnace.py). Печі й верстати лежать в ОДНОМУ модулі саме тому:
порядок видно у файлі й він не залежить від порядку include_router().
"""

from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.models import Furnace, Machine
from app.routers.deps import get_db
from app.machine_portraits import PortraitError, delete_portrait, save_portrait
from app.services import machines as machines_service
from app.settings_store import set_furnace_background, set_setting
from app.crypto import encrypt_value
from app.services.furnace import FurnaceConfigError, validate_address
from .common import require_settings_admin

router = APIRouter()


@router.post("/settings/furnaces")
def add_furnace(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Додати пічку в перелік.

    Адреса перевіряється ДО збереження: криво написаний рядок краще відбити
    тут, ніж потім показувати оператору порожню плитку «немає зв'язку».
    """
    require_settings_admin(request, db)
    try:
        clean_host, clean_port = validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#furnaces", status_code=303)

    if db.scalar(
        select(Furnace).where(Furnace.host == clean_host, Furnace.port == clean_port)
    ):
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Пічка {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#furnaces", status_code=303)

    last = db.scalar(select(func.max(Furnace.sort_order)))
    db.add(
        Furnace(
            name=name.strip() or clean_host,
            host=clean_host,
            port=clean_port,
            enabled=True,
            password_encrypted=encrypt_value(password.strip()) if password.strip() else None,
            sort_order=(last or 0) + 1,
            created_at=datetime.now(),
        )
    )
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Пічку «{name.strip() or clean_host}» додано.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


# Літеральний шлях мусить стояти ПЕРЕД параметризованим: FastAPI приміряє
# маршрути в порядку оголошення, і /settings/furnaces/{furnace_id} нижче радо
# з'їдав «password» як номер пічки (спіймано живою перевіркою — 422 замість
# збереження пароля).
@router.post("/settings/furnaces/background")
def toggle_furnace_background(
    request: Request, enabled: str = Form(""), db: Session = Depends(get_db)
):
    """Увімкнути або вимкнути фотографію-фон на екрані «Пічки».

    Оголошено ВИЩЕ /settings/furnaces/{furnace_id} з тієї ж причини, що й
    /password: FastAPI приміряє маршрути в порядку оголошення й з'їв би слово
    «background» як номер пічки.
    """
    require_settings_admin(request, db)
    set_furnace_background(db, enabled == "1")
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": "Фон екрана «Пічки» увімкнено." if enabled == "1" else "Фон екрана «Пічки» вимкнено.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


@router.post("/settings/furnaces/password")
def save_furnace_password(
    request: Request, password: str = Form(""), db: Session = Depends(get_db)
):
    """Спільний пароль VNC. Порожнє поле означає «не міняти» — з тієї ж
    причини, що й у рядку пічки вище."""
    require_settings_admin(request, db)
    if password.strip():
        set_setting(db, "furnace_vnc_password", password.strip())
        db.commit()
        message = "Спільний пароль пічок збережено."
    else:
        message = "Пароль не змінено — поле лишилось порожнім."
    request.session["settings_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings#furnaces", status_code=303)


@router.post("/settings/furnaces/{furnace_id}")
def update_furnace(
    request: Request,
    furnace_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    enabled: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Змінити пічку.

    Порожній пароль означає «не міняти», а не «стерти»: рядок відкривають, щоб
    виправити адресу, і збережений пароль не має зникати від того, що поле не
    заповнили вдруге. Стерти власний пароль можна словом `-` — так є явний
    спосіб повернути пічку на спільний пароль.
    """
    require_settings_admin(request, db)
    furnace = db.get(Furnace, furnace_id)
    if furnace is None:
        raise HTTPException(status_code=404, detail="пічку не знайдено")

    try:
        clean_host, clean_port = validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#furnaces", status_code=303)

    clash = db.scalar(
        select(Furnace).where(
            Furnace.host == clean_host, Furnace.port == clean_port, Furnace.id != furnace_id
        )
    )
    if clash is not None:
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Пічка {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#furnaces", status_code=303)

    furnace.name = name.strip() or clean_host
    furnace.host = clean_host
    furnace.port = clean_port
    furnace.enabled = enabled == "1"
    if password.strip() == "-":
        furnace.password_encrypted = None
    elif password.strip():
        furnace.password_encrypted = encrypt_value(password.strip())
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Пічку «{furnace.name}» збережено.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


@router.post("/settings/furnaces/{furnace_id}/delete")
def delete_furnace(request: Request, furnace_id: int, db: Session = Depends(get_db)):
    """Прибрати пічку з переліку. Її показання лишаються в історії — рядки
    підписані адресою, і чистити їх разом із записом означало б втратити те,
    що вже сталося."""
    require_settings_admin(request, db)
    furnace = db.get(Furnace, furnace_id)
    if furnace is None:
        raise HTTPException(status_code=404, detail="пічку не знайдено")
    name = furnace.name
    db.delete(furnace)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Пічку «{name}» прибрано з переліку.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


# ── Верстати ────────────────────────────────────────────────────────────────
# Дзеркало роутів пічок вище, включно з ПАСТКОЮ ПОРЯДКУ: літеральний
# /settings/machines/password мусить стояти ПЕРЕД /settings/machines/{id},
# інакше FastAPI з'їдає слово «password» як номер верстата (спіймано живою
# перевіркою на пічках — 422 замість збереження).


@router.post("/settings/machines")
def add_machine(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    password: str = Form(""),
    agent_token: str = Form(""),
    portrait_model: str = Form(""),
    db: Session = Depends(get_db),
):
    """Додати верстат. Адреса перевіряється ДО збереження — криву краще
    відбити тут, ніж показувати порожню плитку «немає зв'язку»."""
    require_settings_admin(request, db)
    # HTTP-агент типово на 8765; VNC — на 5900. Якщо порт не вказано, беремо
    # за замовчуванням той, що відповідає обраному способу.
    if not port.strip():
        port = "8765" if agent_token.strip() else ""
    try:
        clean_host, clean_port = machines_service.validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#machines", status_code=303)

    if db.scalar(
        select(Machine).where(Machine.host == clean_host, Machine.port == clean_port)
    ):
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Верстат {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#machines", status_code=303)

    last = db.scalar(select(func.max(Machine.sort_order)))
    db.add(
        Machine(
            name=name.strip() or clean_host,
            host=clean_host,
            port=clean_port,
            enabled=True,
            password_encrypted=encrypt_value(password.strip()) if password.strip() else None,
            agent_token_encrypted=encrypt_value(agent_token.strip()) if agent_token.strip() else None,
            sort_order=(last or 0) + 1,
            portrait_model=portrait_model if portrait_model in machines_service.MACHINE_MODEL_KEYS else "",
            created_at=datetime.now(),
        )
    )
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Верстат «{name.strip() or clean_host}» додано.",
    }
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/password")
def save_machine_password(
    request: Request, password: str = Form(""), db: Session = Depends(get_db)
):
    """Спільний view-only пароль UltraVNC верстатів. Порожнє = не міняти."""
    require_settings_admin(request, db)
    if password.strip():
        set_setting(db, "machine_vnc_password", password.strip())
        db.commit()
        message = "Спільний пароль верстатів збережено."
    else:
        message = "Пароль не змінено — поле лишилось порожнім."
    request.session["settings_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/{machine_id}")
def update_machine(
    request: Request,
    machine_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    enabled: str = Form(""),
    password: str = Form(""),
    agent_token: str = Form(""),
    collect_calibration: str = Form(""),
    portrait_model: str = Form(""),
    db: Session = Depends(get_db),
):
    """Змінити верстат. Порожній пароль/токен = не міняти; `-` = стерти
    (той самий контракт, що в пічки)."""
    require_settings_admin(request, db)
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail="верстат не знайдено")

    try:
        clean_host, clean_port = machines_service.validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#machines", status_code=303)

    clash = db.scalar(
        select(Machine).where(
            Machine.host == clean_host, Machine.port == clean_port, Machine.id != machine_id
        )
    )
    if clash is not None:
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Верстат {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#machines", status_code=303)

    machine.name = name.strip() or clean_host
    machine.host = clean_host
    machine.port = clean_port
    machine.enabled = enabled == "1"
    if password.strip() == "-":
        machine.password_encrypted = None
    elif password.strip():
        machine.password_encrypted = encrypt_value(password.strip())
    if agent_token.strip() == "-":
        machine.agent_token_encrypted = None
    elif agent_token.strip():
        machine.agent_token_encrypted = encrypt_value(agent_token.strip())
    machine.collect_calibration = collect_calibration == "1"
    # Невідомий ключ = «авто» (здогад за назвою), а не помилка: форма шле лише
    # свої чотири варіанти, чужий може прийти хіба зі старої вкладки.
    machine.portrait_model = portrait_model if portrait_model in machines_service.MACHINE_MODEL_KEYS else ""
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Верстат «{machine.name}» збережено.",
    }
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/{machine_id}/portrait")
async def upload_machine_portrait(
    request: Request, machine_id: int, photo: UploadFile = File(...), db: Session = Depends(get_db)
):
    """Фото верстата для картки на екрані «Верстати». Формат і розмір
    перевіряє machine_portraits (не розширення від браузера)."""
    require_settings_admin(request, db)
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail="верстат не знайдено")
    try:
        save_portrait(machine.id, photo.file)
    except PortraitError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#machines", status_code=303)
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Фото верстата «{machine.name}» збережено.",
    }
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/{machine_id}/portrait/delete")
def delete_machine_portrait(request: Request, machine_id: int, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail="верстат не знайдено")
    delete_portrait(machine.id)
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Фото верстата «{machine.name}» прибрано — картка знову з портретом моделі.",
    }
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/{machine_id}/delete")
def delete_machine(request: Request, machine_id: int, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail="верстат не знайдено")
    name = machine.name
    delete_portrait(machine.id)
    db.delete(machine)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Верстат «{name}» прибрано з переліку.",
    }
    return RedirectResponse("/settings#machines", status_code=303)
