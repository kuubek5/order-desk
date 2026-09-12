"""Екран «Скринька невідомих екранів»: кадр, якого читач не зрозумів.

Читач екранів печей і верстатів мовчить при найменшому сумніві — цифра або
збігається з еталоном піксель-у-піксель, або поле порожнє. Правило добре, але
мовчання нікуди не веде: щоб порожнє поле колись заповнилось, треба знати, ЯКИЙ
це був екран. Скринька (`app/services/screen_inbox.py`) відкладає такий кадр
одним рядком на відпечаток; цей екран показує відкладене й приймає ОДИН рядок
від людини.

**Підпис — дані, а не правило.** Через цей екран застосунок нічого не вчиться:
зону, еталон чи правило статусу з підпису роблять у репозиторії, з тестом на
тому ж кадрі (один кривий еталон псує читання назовсім, і видно це не одразу).
Тому напис про це стоїть просто над полем підпису, а не в довідці: оператор,
який вважає, що «розмітив — і воно запрацювало», чекатиме дива й не скаже про
проблему вголос.

Домен — у сервісі; тут лише HTTP. Роути НЕ будують шлях до картинки з
параметра адреси: `screen_inbox.image_path` бере його з РЯДКА бази за id, тож
у адресному рядку немає нічого, що вело б до чужого файлу.

Фрагменти, які свапає екран:
  * `#sinb-board`  — лічильники + список (після «неважливо» і перемикання
                     показу відкладених);
  * `#sinb-row-<id>` — один рядок (після збереження підпису; решта полів на
                     екрані при цьому не смикається);
  * `#sinb-counts` — лічильники приїжджають слідом за рядком через
                     `hx-swap-oob`, бо підпис міняє «без підпису N».
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import ScreenPuzzle
from app.routers.deps import get_current_user, get_db, login_redirect, templates
from app.services import screen_inbox

router = APIRouter()


def _kind_word(kind: str) -> str:
    return "піч" if kind == screen_inbox.KIND_FURNACE else "верстат"


def _puzzle(db: Session, puzzle_id: int) -> ScreenPuzzle:
    puzzle = db.get(ScreenPuzzle, puzzle_id)
    if puzzle is None:
        # Загадку могло витіснити стелею скриньки, поки екран був відкритий.
        raise HTTPException(status_code=404, detail="загадки вже немає")
    return puzzle


def _image_flags(puzzle: ScreenPuzzle) -> dict:
    """Чи є файл кадру/вирізу НА ДИСКУ — рахує роутер, не шаблон.

    `app/backup.py` свідомо копіює рядок `screen_puzzles`, а не сам файл
    кадру («рядок без картинки далі показує причину, подробиці й підпис»),
    тож після відновлення на новому ПК файлу може не бути, хоча
    `frame_file`/`zone_file` у рядку заповнені. Перевірка файлової системи —
    ОДНА на рядок тут, а не циклом по всіх рядках усередині шаблону.
    """
    return {
        "has_frame": screen_inbox.image_path(puzzle, "frame") is not None,
        "has_zone": screen_inbox.image_path(puzzle, "zone") is not None,
    }


def _board(request: Request, db: Session, user, show_dismissed: bool) -> dict:
    """Контекст лічильників і списку — один на сторінку й на фрагмент."""
    puzzles = screen_inbox.listing(db, include_dismissed=show_dismissed)
    return {
        "request": request,
        "user": user,
        "topbar_active": "screens",
        "puzzles": puzzles,
        "image_flags": {p.id: _image_flags(p) for p in puzzles},
        "counts": screen_inbox.counts(db),
        "reasons": screen_inbox.REASONS,
        "kind_word": _kind_word,
        "show_dismissed": show_dismissed,
    }


def _row(request: Request, puzzle: ScreenPuzzle, db: Session, *, saved: bool, show: bool):
    """Один рядок + лічильники слідом (hx-swap-oob).

    Свапати цілу дошку після підпису не можна: сусідні поля вводу тоді
    перемальовуються разом із текстом, який хтось саме набирає. `show` їде
    через форму й повертається в розмітку рядка — інакше кнопка «неважливо»
    в щойно підписаному рядку перемкнула б вигляд усього списку.
    """
    return templates.TemplateResponse(
        request,
        "_screen_inbox_row.html",
        {
            "request": request,
            "p": puzzle,
            "reasons": screen_inbox.REASONS,
            "kind_word": _kind_word,
            "image_flags": {puzzle.id: _image_flags(puzzle)},
            "counts": screen_inbox.counts(db),
            "oob_counts": True,
            "saved": saved,
            "show_dismissed": show,
        },
    )


def _toast(response, message: str, kind: str = "success"):
    """Тост поверх свапу.

    `ensure_ascii` лишається ввімкненим (за замовчуванням): значення HTTP-
    заголовка — latin-1, і сира кирилиця в ньому валила весь запит у 500.
    """
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": message, "kind": kind}})
    return response


# ── Сторінка й фрагмент ─────────────────────────────────────────────────────


@router.get("/screens", response_class=HTMLResponse)
def screens_page(request: Request, dismissed: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request, "screens.html", _board(request, db, user, bool(dismissed))
    )


@router.get("/screens/board", response_class=HTMLResponse)
def screens_board(request: Request, dismissed: int = 0, db: Session = Depends(get_db)):
    """Дошка окремо — перемикач «показати неважливі» і відповідь на дію."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    return templates.TemplateResponse(
        request, "_screen_inbox_board.html", _board(request, db, user, bool(dismissed))
    )


# ── Картинки ────────────────────────────────────────────────────────────────


def _image(request: Request, db: Session, puzzle_id: int, which: str) -> FileResponse:
    """Кадр або виріз зони.

    Шлях НЕ будується з адреси: у ній лише id рядка, а ім'я файлу й тека
    беруться з самого рядка (`screen_inbox.image_path`). Тому написане в
    адресному рядку нікуди, крім скриньки, не веде.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    path = screen_inbox.image_path(_puzzle(db, puzzle_id), which)
    if path is None:
        raise HTTPException(status_code=404, detail="картинки немає")
    # no-store: файл лежить під іменем-відпечатком і теоретично сталий, але
    # витіснення звільняє id, і кешована картинка показувала б чужу загадку.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/screens/{puzzle_id}/frame.png")
def screen_frame(request: Request, puzzle_id: int, db: Session = Depends(get_db)):
    return _image(request, db, puzzle_id, "frame")


@router.get("/screens/{puzzle_id}/zone.png")
def screen_zone(request: Request, puzzle_id: int, db: Session = Depends(get_db)):
    return _image(request, db, puzzle_id, "zone")


# ── Дії ─────────────────────────────────────────────────────────────────────


@router.post("/screens/{puzzle_id}/label", response_class=HTMLResponse)
def screen_label(
    request: Request,
    puzzle_id: int,
    label: str = Form(""),
    show: str = Form("0"),
    db: Session = Depends(get_db),
):
    """Підпис людини одним рядком.

    Порожнє поле тут однозначне (на відміну від пастки §14 «порожнє поле =
    поля не було»): і пропущене поле, і стерте означають одне — підпису
    більше немає, і сервіс так само знімає `labeled_at`. Другого сенсу, який
    треба було б розрізняти, у цій формі немає.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    puzzle = _puzzle(db, puzzle_id)
    screen_inbox.set_label(db, puzzle.id, label, user_id=user.id)
    saved = bool(puzzle.label)
    return _toast(
        _row(request, puzzle, db, saved=saved, show=show in ("1", "on", "true")),
        "Підпис збережено — він поїде розробнику" if saved else "Підпис прибрано",
    )


@router.post("/screens/{puzzle_id}/dismiss", response_class=HTMLResponse)
def screen_dismiss(
    request: Request,
    puzzle_id: int,
    dismissed: str = Form("on"),
    show: str = Form("0"),
    db: Session = Depends(get_db),
):
    """«Це неважливо» і повернення назад.

    Рядок не зникає з бази: лічильник «бачено N разів» відповідає на «як часто
    таке буває», і саме частота колись може перетворити «неважливо» на
    «розберись». Але при переповненні скриньки він іде першим.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    puzzle = _puzzle(db, puzzle_id)
    off = dismissed not in ("on", "1", "true")
    screen_inbox.set_dismissed(db, puzzle.id, not off)
    return _toast(
        templates.TemplateResponse(
            request,
            "_screen_inbox_board.html",
            _board(request, db, user, show in ("1", "on", "true")),
        ),
        "Повернуто в скриньку" if off else "Позначено «неважливо»",
    )
