"""Екран «Зміна» — дошка передачі між нічними змінами.

Нічний оператор іде о ~05:00, наступний приходить о ~08:00. Три години без
людей у цеху зараз закриваються СМС-ками: печі, стан верстатів, «цю не
запускай». Тут це записки на дошці поточної ночі, нижче — минулі ночі списком.

Доменна логіка (кому лишатись на дошці, що скидає прийняття, як групувати
ніч) живе в app/services/shift.py; тут — лише HTTP.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import ShiftNote
from app.routers.deps import get_current_user, get_db, templates, toast_response
from app.services.shift import (
    KIND_INFO,
    ShiftNoteError,
    acknowledge,
    create_note,
    edit_note,
    feed,
    group_by_night,
    night_label,
    open_notes,
    resolve,
)

router = APIRouter()


def _shift_context(request: Request, db: Session, user) -> dict:
    """Спільний контекст дошки: поточна ніч зверху, минулі — списком нижче."""
    board = open_notes(db)
    rows, truncated = feed(db)
    open_ids = {n.id for n in board}
    # Минулі ночі — усе, що вже пішло з дошки. Ніч, у якій ще щось відкрите,
    # цілком лишається дошкою: розривати одну передачу навпіл не можна.
    open_nights = {group[0] for group in group_by_night(board)}
    history = [
        (start, notes)
        for start, notes in group_by_night([n for n in rows if n.id not in open_ids])
        if start not in open_nights
    ]
    return {
        "request": request,
        "user": user,
        "board": board,
        "history": history,
        "history_truncated": truncated,
        "tonight": night_label(
            next(iter(open_nights)) if open_nights else _tonight_start()
        ),
    }


def _tonight_start():
    from app.services.shift import night_of

    return night_of(datetime.now())


@router.get("/shift", response_class=HTMLResponse)
def get_shift(request: Request, partial: str = "", db: Session = Depends(get_db)):
    """Дошка передачі зміни. `partial=board` віддає саму дошку — цим свапом
    відповідають на «Прийняв»/«Прийнято»/редагування, тому поле написання
    свідомо лежить ПОЗА цим фрагментом: інакше свап зітер би недописаний текст
    колеги (та сама пастка, через яку полл черги свапає рівно `#queue-rows`)."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    context = _shift_context(request, db, user)
    if partial == "board":
        return templates.TemplateResponse(request, "_shift_board.html", context)

    context["toast_flash"] = request.session.pop("toast_flash", None)
    return templates.TemplateResponse(request, "shift.html", context)


def _require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return user


def _require_note(db: Session, note_id: int) -> ShiftNote:
    note = db.get(ShiftNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="записку не знайдено")
    return note


@router.post("/shift/notes")
def create_shift_note(
    request: Request,
    text: str = Form(""),
    kind: str = Form(KIND_INFO),
    db: Session = Depends(get_db),
):
    """Написати записку. Звичайний 303-редірект, не HTMX: тут же поїдуть
    файли (multipart), і форма має працювати без JS — як /orders/new."""
    user = _require_user(request, db)
    try:
        create_note(db, kind=kind, text=text, author=user)
    except ShiftNoteError as exc:
        request.session["toast_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/shift", status_code=303)

    db.commit()
    request.session["toast_flash"] = {
        "kind": "success",
        "message": (
            "Записку пришпилено."
            if kind == KIND_INFO
            else "Записку пришпилено — вона лишиться на дошці до закриття."
        ),
    }
    return RedirectResponse("/shift", status_code=303)


# Чому ці три віддають toast_response із HX-Trigger, а не готовий фрагмент: та
# сама кнопка живе у двох місцях (дошка /shift і картка на черзі) з різними
# цілями свапу, а роут може повернути лише одну форму. Подія `refresh-shift`
# дає кожному місцю перечитати себе самому.
@router.post("/shift/notes/{note_id}/ack")
def ack_shift_note(request: Request, note_id: int, db: Session = Depends(get_db)):
    """«Прийняв» — прочитано. Одне на записку: перший, хто натиснув, закриває
    для всіх, тож повторне натискання нічого не переписує."""
    user = _require_user(request, db)
    note = _require_note(db, note_id)

    changed = acknowledge(db, note, user=user)
    db.commit()

    name = (note.acknowledged_by.full_name or note.acknowledged_by.username) if note.acknowledged_by else "—"
    return toast_response(
        "Прийнято." if changed else f"Записку вже прийняв {name}.",
        kind="success" if changed else "info",
        triggers={"refresh-shift": True},
    )


@router.post("/shift/notes/{note_id}/resolve")
def resolve_shift_note(request: Request, note_id: int, db: Session = Depends(get_db)):
    """Закрити записку «потребує дії» — справу зроблено, вона йде з дошки."""
    user = _require_user(request, db)
    note = _require_note(db, note_id)

    try:
        changed = resolve(db, note, user=user)
    except ShiftNoteError as exc:
        return toast_response(str(exc), kind="error")

    db.commit()
    return toast_response(
        "Закрито." if changed else "Записку вже закрито.",
        kind="success" if changed else "info",
        triggers={"refresh-shift": True},
    )


@router.post("/shift/notes/{note_id}/edit")
def edit_shift_note(
    request: Request,
    note_id: int,
    text: str = Form(""),
    db: Session = Depends(get_db),
):
    """Змінити текст. Редагує лише автор — і зміна СКИДАЄ прийняття (див.
    сервіс): інакше колега прийняв один текст, а на дошці висить інший."""
    user = _require_user(request, db)
    note = _require_note(db, note_id)
    if note.author_id != user.id:
        return toast_response("Редагувати може лише автор записки.", kind="error")

    was_acknowledged = note.acknowledged_at is not None
    try:
        edit_note(db, note, text=text)
    except ShiftNoteError as exc:
        return toast_response(str(exc), kind="error")

    db.commit()
    message = "Записку змінено."
    if was_acknowledged and note.acknowledged_at is None:
        message = "Записку змінено — прийняття скинуто, треба прийняти наново."
    return toast_response(message, triggers={"refresh-shift": True})
