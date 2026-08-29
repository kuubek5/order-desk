"""Екран «Печі» — тільки перегляд стану печей спікання.

Перегляд означає НАШ вигляд: кадр табло плюс розпізнані числа, а не жива
трансляція екрана. Живий екран без керування нічого не додає (керувати
однаково не можна), а кадр раз на кілька секунд — це рівно те, за чим ходять
до печі: працює чи ні, скільки лишилось, яка температура.

Керування піччю тут відсутнє свідомо і повністю: у застосунку немає коду, який
шле печі байт вводу (див. app/furnace_vnc.py). Скасувати програму на 1500 °C
одним кліком у браузері — аварія, а не зручність.

Доменна логіка (коли опитувати, коли писати в базу, як зберігати кадр) живе в
app/services/furnace.py; тут — лише HTTP.
"""

import io
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.furnace_ocr import EYE_CROPS
from app.routers.deps import get_current_user, get_db, templates
from app.services.furnace import (
    POLL_INTERVAL_SECONDS,
    config_error,
    eye_crop,
    poll_all,
    resolve_frame,
    snapshot,
    strip_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _context(request: Request, db: Session, user) -> dict:
    return {
        "request": request,
        "user": user,
        "topbar_active": "furnaces",
        "cards": snapshot(db),
        "config_error": config_error(db),
        "poll_seconds": int(POLL_INTERVAL_SECONDS),
    }


@router.get("/furnaces", response_class=HTMLResponse)
def furnaces_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "furnaces.html", _context(request, db, user))


@router.get("/furnaces/cards", response_class=HTMLResponse)
def furnaces_cards(request: Request, db: Session = Depends(get_db)):
    """Фрагмент для полла HTMX.

    Плитки перемальовуються цілком — на цьому екрані немає нічого, що оператор
    міг би тримати під курсором і втратити при свапі (жодного поля вводу, жодної
    дії над рядком). Це той випадок, коли повний свап простий і безпечний.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(request, "_furnace_cards.html", _context(request, db, user))


@router.get("/furnaces/strip", response_class=HTMLResponse)
def furnaces_strip(request: Request, db: Session = Depends(get_db)):
    """Смуга печей над чергою — на власному 30-секундному годиннику.

    Обгортку віддаємо ЗАВЖДИ, навіть порожню: якби на «печей немає» роут
    повертав нічого, елемент зник би з DOM разом зі своїм поллом, і після
    налаштування печей смуга більше ніколи б не проступила (той самий урок,
    що на картці передачі зміни).

    Читає ЛИШЕ стан у памʼяті процесу — жодного підключення до печі з потоку,
    який обслуговує запит: інакше мовчазна піч тримала б чергу 20 секунд.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    cards = snapshot(db)
    return templates.TemplateResponse(
        request,
        "_furnace_strip.html",
        {"furnace_cards": cards, "furnace_summary": strip_summary(cards)},
    )


@router.post("/furnaces/refresh", response_class=HTMLResponse)
def furnaces_refresh(request: Request, db: Session = Depends(get_db)):
    """Зняти кадр просто зараз, не чекаючи фонового тіку.

    Це ЧИТАННЯ, а не дія над піччю: кнопка лише прискорює наш власний знімок.
    Тому вона доступна оператору й не питає підтвердження.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    poll_all(db)
    return templates.TemplateResponse(request, "_furnace_cards.html", _context(request, db, user))


@router.get("/furnaces/{key}/frame.png")
def furnace_frame(request: Request, key: str, db: Session = Depends(get_db)):
    """Останній кадр табло.

    `key` НЕ підставляється у шлях: він звіряється зі станом процесу, і шлях
    будується з відомої печі (див. services.furnace.resolve_frame). Невідомий
    ключ дає 404, а не спробу відкрити те, що написали в адресному рядку.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    path = resolve_frame(key)
    if path is None:
        raise HTTPException(status_code=404, detail="кадру ще немає")
    # Кадр перезаписується під тим самим іменем — без no-store браузер показував
    # би вчорашню картинку, і екран «оновлювався» б, не змінюючись.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/furnaces/{key}/crop/{name}.png")
def furnace_crop(request: Request, key: str, name: str, db: Session = Depends(get_db)):
    """Смужка кадру для звірки очима.

    Головний запобіжник усього екрана: поруч із розпізнаним числом видно
    справжні пікселі табло. Якщо розпізнавання колись помилиться, це буде видно
    одразу, а не після зіпсованої партії.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if name not in EYE_CROPS:
        raise HTTPException(status_code=404, detail="невідома зона")
    image = eye_crop(key, name)
    if image is None:
        raise HTTPException(status_code=404, detail="кадру ще немає")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
