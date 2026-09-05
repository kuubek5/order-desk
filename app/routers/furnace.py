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
from app.routers.deps import get_current_user, login_redirect, get_db, templates
from app.settings_store import get_furnace_background
from app.services.furnace import (
    POLL_INTERVAL_SECONDS,
    all_idle,
    config_error,
    configured_targets,
    eye_crop,
    poll_all,
    resolve_frame,
    snapshot,
    strip_cards,
    strip_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Від якої температури фон починає жевріти й на якій сягає повної сили.
# Нижче 200° пічка на око холодна, вище 1500° яскравіше вже нема куди.
HEAT_FROM_C = 200
HEAT_TO_C = 1500


def _heat(cards) -> float:
    """Сила зарева на фоні: 0…1 за найгарячішою пічкою, що зараз працює.

    Саме за працюючою, а не за будь-якою: пічка, що просто не встигла охолонути
    після відкриття, не має підсвічувати екран так, ніби в ній іде програма.
    Фон тут не прикраса — він каже те саме, що числа, тільки бічним зором.
    """
    temps = [c.state.temp_c for c in cards if c.is_running and c.state and c.state.temp_c]
    if not temps:
        return 0.0
    hottest = max(temps)
    return round(min(1.0, max(0.0, (hottest - HEAT_FROM_C) / (HEAT_TO_C - HEAT_FROM_C))), 3)


def _context(request: Request, db: Session, user) -> dict:
    cards = snapshot(db)
    return {
        "request": request,
        "user": user,
        "topbar_active": "furnaces",
        "cards": cards,
        "config_error": config_error(db),
        "poll_seconds": int(POLL_INTERVAL_SECONDS),
        "furnace_bg": get_furnace_background(db),
        "furnace_heat": _heat(cards),
        # Стан фону: закрита пічка означає «зараз щось печеться».
        "furnace_firing": any(card.is_running for card in cards),
    }


@router.get("/furnaces", response_class=HTMLResponse)
def furnaces_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
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


def _side_context(db: Session, cards) -> dict:
    """Спільний контекст смуги й секції.

    `furnaces_configured` потрібен, щоб порожній стан не брехав: `strip_cards`
    свідомо не пускає ще не опитану піч, тому одразу після рестарту список
    порожній — і секція писала «Печей не налаштовано», хоча вони налаштовані.
    Оператор ішов у налаштування шукати проблему, якої немає, або додавав
    дубль печі.
    """
    return {
        "furnace_cards": cards,
        "furnace_summary": strip_summary(cards),
        "furnaces_configured": len(configured_targets(db)),
        "furnaces_all_idle": all_idle(cards),
    }


@router.get("/furnaces/side", response_class=HTMLResponse)
def furnaces_side(request: Request, db: Session = Depends(get_db)):
    """Секція «Пічки» в бічній панелі черги — власний 30-секундний годинник.

    Той самий контракт, що й у смуги: обгортку віддаємо ЗАВЖДИ (інакше на
    «печей немає» елемент зник би з DOM разом зі своїм поллом), і читаємо
    ЛИШЕ стан у пам'яті процесу — до печі з потоку запиту не ходимо, бо
    мовчазна піч тримала б чергу двадцять секунд.

    Дані беруться з тих самих strip_cards/strip_summary, що годують смугу:
    два погляди на одне значення — нормально, два джерела — ні.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    cards = strip_cards(db)
    return templates.TemplateResponse(
        request,
        "_furnace_side.html",
        _side_context(db, cards),
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
