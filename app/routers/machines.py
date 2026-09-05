"""Екран «Верстати» — живі кадри екранів RemiCORE, тільки перегляд.

Фаза 1: кадр раз на кілька секунд, без розпізнавання чисел. Це вже закриває
головний сценарій — «глянути, що на верстаті, не відкриваючи RustDesk».
Фаза 2 (OCR відсотка/часу/програми) додасться поверх цих самих кадрів.

Керування верстатом відсутнє свідомо й повністю: знімок іде через
app/furnace_vnc.py, який фізично не вміє слати ввід (перевірено стендом).
Доменна логіка — app/services/machines.py; тут лише HTTP.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.machine_portraits import portrait_path
from app.routers.deps import get_current_user, login_redirect, get_db, is_loopback_request, templates
from app.services.machines import (
    POLL_INTERVAL_SECONDS,
    calibration_status,
    calibration_zip_bytes,
    machine_side_context,
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
        # Банер калібрування: показується, лише доки шрифт підпису неповний.
        "calibration": calibration_status(),
    }


@router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    return templates.TemplateResponse(request, "machines.html", _context(request, db, user))


@router.get("/machines/side", response_class=HTMLResponse)
def machines_side(request: Request, db: Session = Depends(get_db)):
    """Секція «Верстати» в бічній панелі черги — власний 30-секундний годинник.

    Читає ЛИШЕ стан у пам'яті процесу: до верстата з потоку запиту не ходимо,
    бо мовчазний ПК тримав би чергу двадцять секунд. Обгортку віддаємо ЗАВЖДИ
    (навіть без верстатів), інакше елемент зник би з DOM разом зі своїм поллом.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(
        request, "_machine_side.html", machine_side_context(db)
    )


@router.get("/machines/strip", response_class=HTMLResponse)
def machines_strip(request: Request, db: Session = Depends(get_db)):
    """Стрічка «назва + %» над чергою — власний 10-секундний годинник.

    Той самий контракт, що в /machines/side: лише пам'ять процесу, обгортка
    завжди, контекст з machine_side_context — два входи не розійдуться.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(
        request, "_machine_strip.html", machine_side_context(db)
    )


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


@router.get("/machines/calibration.zip")
def machines_calibration_zip(request: Request, db: Session = Depends(get_db)):
    """Скачати всі зібрані калібрувальні кадри одним zip.

    Оголошено ВИЩЕ за `/machines/{key}/frame.png`: FastAPI приміряє роути в
    порядку оголошення, і параметричний з'їв би «calibration» як ключ (та сама
    пастка, що з паролем печі — є тест-сторож).

    Адмін + лише з цього ПК: це обслуговуюча дія над локальним диском, як і
    решта дій рівня машини (відкрити теку, оновлення)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    data = calibration_zip_bytes()
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="calibration_frames.zip"'},
    )


@router.get("/machines/portrait/{machine_id}.jpg")
def machine_portrait(request: Request, machine_id: int, db: Session = Depends(get_db)):
    """Фото верстата, завантажене в Налаштуваннях. Шлях будується з числа,
    а не з рядка запиту; немає файлу — 404, картка тоді бере дефолт моделі."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    path = portrait_path(machine_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="фото немає")
    # У URL є mtime (?v=), тому кешувати можна довго: нове фото = новий URL.
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


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
