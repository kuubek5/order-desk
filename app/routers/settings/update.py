"""Оновлення застосунку: перевірка релізу, стан установки, запуск інсталятора."""

import logging
from threading import Thread
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.__version__ import VERSION
from app.changelog import load_changelog
from app.routers.deps import (
    get_current_user,
    login_redirect,
    get_db,
    is_loopback_request,
    templates,
)
from app.update_check import (
    _update_check_tick,
    download_and_verify,
    get_known_update,
    human_update_error,
    install_state,
    launch_silent_install,
    set_install_state,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _install_update_in_background(release) -> None:
    """Runs on its own daemon thread — download+verify+silent-install can
    take a while (network download, then Inno Setup itself), and the HTTP
    response to the admin's click must not block on any of that."""
    version = release.version
    set_install_state(stage="downloading", version=version, done=0, total=0)
    try:
        installer_path = download_and_verify(
            release,
            progress=lambda done, total: set_install_state(
                stage="downloading", version=version, done=done, total=total
            ),
        )
        set_install_state(stage="launching", version=version)
        launch_silent_install(installer_path)
        # Далі — watchdog: інсталятор, перезапуск, /health. Оверлей чекає на
        # падіння й повернення сервера, як і раніше.
        set_install_state(stage="launched", version=version)
    except Exception as exc:
        logger.exception("Background update install failed for release %s", version)
        # Оверлей мусить побачити збій, інакше висить вічно.
        set_install_state(stage="failed", version=version, message=human_update_error(exc))


@router.post("/settings/update/check", response_class=HTMLResponse)
def check_update(request: Request, db: Session = Depends(get_db)):
    """Admin-triggered manual "is there a newer build?" probe for the settings
    "Про застосунок" section. Runs the same one-shot check the daily background
    worker does (_update_check_tick, which refreshes the module-level "last
    known release" that the rail banner also reads), then renders the result as
    an HTMX fragment: an install button when a newer version is found, or a
    reassuring "you're on the latest" otherwise. Network failures never surface
    raw errors — fetch_latest_release swallows them and returns None, same as
    the background path.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    reached = _update_check_tick()
    return templates.TemplateResponse(
        request,
        "_update_check_result.html",
        {"release": get_known_update(), "current_version": VERSION, "reached": reached},
    )


@router.get("/settings/changelog", response_class=HTMLResponse)
def changelog_full(request: Request, db: Session = Depends(get_db)):
    """Увесь журнал змін одним фрагментом — за кліком, не в кожному /settings.

    Розділ «Про застосунок» бачать усі ролі, тож і фрагмент — для будь-кого,
    хто увійшов. Аудит 06.09.26: повний список (114 релізів, 225 КБ) їхав у
    кожне відкриття налаштувань і робив сторінку важчою за чергу.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(
        request,
        "_changelog_list.html",
        {"changelog": load_changelog(), "changelog_more": False},
    )


@router.get("/settings/update/status")
def update_install_status(request: Request, db: Session = Depends(get_db)):
    """Стан встановлення для оверлею: стадія, байти, причина збою.

    Читає лише пам'ять процесу. Ті самі ворота, що й у /update/install: це
    той самий адмін на тому самому ПК, і чужому оку тут нічого робити.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    return JSONResponse(install_state(), headers={"Cache-Control": "no-store"})


@router.post("/settings/update/install")
def install_update(request: Request, db: Session = Depends(get_db)):
    """Admin-triggered install of the update already found by the
    background checker (app/update_check.py). Downloads, verifies the
    checksum, and launches the silent installer + relaunch watchdog on a
    background thread — see _install_update_in_background above and
    launch_silent_install's docstring for the skipifsilent workaround.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    release = get_known_update()
    if release is None:
        request.session["settings_flash"] = {"kind": "error", "message": "Оновлень немає"}
        return RedirectResponse("/settings", status_code=303)

    try:
        Thread(
            target=_install_update_in_background,
            args=(release,),
            name="order-desk-update-install",
            daemon=True,
        ).start()
    except Exception:
        logger.exception("Failed to start update install thread for release %s", release.version)
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Не вдалося запустити встановлення оновлення",
        }
        return RedirectResponse("/settings", status_code=303)

    request.session["settings_flash"] = {
        "kind": "success",
        "message": "Оновлення встановлюється, застосунок автоматично перезапуститься за кілька секунд",
    }
    return RedirectResponse("/settings", status_code=303)
