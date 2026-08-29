"""Екран «Зміна» — дошка передачі між нічними змінами.

Нічний оператор іде о ~05:00, наступний приходить о ~08:00. Три години без
людей у цеху зараз закриваються СМС-ками: печі, стан верстатів, «цю не
запускай». Тут це записки на дошці поточної ночі, нижче — минулі ночі списком.

Доменна логіка (кому лишатись на дошці, що скидає прийняття, як групувати
ніч) живе в app/services/shift.py; тут — лише HTTP.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import ShiftNote, ShiftNoteImage
from app.shift_images import (
    PRUNE_AFTER_DAYS,
    ShiftImageError,
    analyze_shift_images,
    delete_image,
    media_type_for,
    prune_shift_images,
    resolve_image_file,
    save_image,
)
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
    відповідають на «Прийняв»/«Закрито»/редагування, тому поле написання
    свідомо лежить ПОЗА цим фрагментом: інакше свап зітер би недописаний текст
    колеги (та сама пастка, через яку полл черги свапає рівно `#queue-rows`)."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    context = _shift_context(request, db, user)
    # Звіт про місце — лише адміну й лише на повній сторінці: він обходить
    # теку, і платити за це на кожному свапі дошки нема за що.
    if partial != "board" and user.role == "адмін":
        context["images_report"] = analyze_shift_images(db)
        context["images_prune_days"] = PRUNE_AFTER_DAYS
    if partial == "board":
        return templates.TemplateResponse(request, "_shift_board.html", context)

    context["toast_flash"] = request.session.pop("toast_flash", None)
    return templates.TemplateResponse(request, "shift.html", context)


@router.get("/shift/card", response_class=HTMLResponse)
def get_shift_card(request: Request, db: Session = Depends(get_db)):
    """Картка передачі зміни над чергою — на власному 60-секундному годиннику.

    Обгортку віддаємо ЗАВЖДИ, навіть порожню: якби на «немає записок» роут
    повертав нічого, елемент зник би з DOM разом зі своїм поллом, і нічна
    записка більше ніколи б тут не проступила.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    return templates.TemplateResponse(
        request, "_shift_card.html", {"shift_open_notes": open_notes(db)}
    )


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


def _attach_images(db: Session, note: ShiftNote, uploads: list[UploadFile]) -> list[str]:
    """Прикріпити скріншоти, повернути перелік причин відмов.

    Текст записки на цей момент УЖЕ закомічено — свідомо. Провалений
    скріншот (завеликий, не картинка) не сміє забрати з собою речення
    «піч №2 відкрити о 9:00»; оператор дізнається про відмову з тоста й
    може довкласти файл окремо.
    """
    problems: list[str] = []
    saved = False
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        try:
            save_image(db, note, stream=upload.file, filename=upload.filename)
            saved = True
        except ShiftImageError as exc:
            problems.append(f"{upload.filename}: {exc}")
        finally:
            upload.file.close()
    if saved:
        db.commit()
    else:
        db.rollback()
    return problems


@router.post("/shift/notes")
def create_shift_note(
    request: Request,
    text: str = Form(""),
    kind: str = Form(KIND_INFO),
    images: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Написати записку. Звичайний 303-редірект, не HTMX: це multipart із
    файлами, і форма мусить працювати без JS — як /orders/new.

    Роут синхронний (`def`, не `async def`) СВІДОМО: FastAPI віддасть його в
    threadpool, тож дискове I/O й Pillow не блокують event loop.
    """
    user = _require_user(request, db)
    try:
        note = create_note(db, kind=kind, text=text, author=user)
    except ShiftNoteError as exc:
        request.session["toast_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/shift", status_code=303)

    # Спершу коміт тексту, потім файли — саме в такому порядку, див. _attach_images.
    db.commit()
    problems = _attach_images(db, note, images or [])

    message = (
        "Записку пришпилено."
        if kind == KIND_INFO
        else "Записку пришпилено — вона лишиться на дошці до закриття."
    )
    if problems:
        message += " Не додано: " + "; ".join(problems)
    request.session["toast_flash"] = {
        "kind": "info" if problems else "success",
        "message": message,
    }
    return RedirectResponse("/shift", status_code=303)


@router.post("/shift/notes/{note_id}/images")
def add_shift_note_images(
    request: Request,
    note_id: int,
    images: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Довкласти скріншот до наявної записки (Ctrl+V вже після відправки)."""
    _require_user(request, db)
    note = _require_note(db, note_id)

    problems = _attach_images(db, note, images or [])
    if problems:
        return toast_response("Не додано: " + "; ".join(problems), kind="error")
    return toast_response("Скріншот додано.", triggers={"refresh-shift": True})


@router.get("/shift/images/{image_id}")
def get_shift_image(request: Request, image_id: int, db: Session = Depends(get_db)):
    """Віддати байти скріншота.

    Шлях перевіряється НАНОВО (app/shift_images.resolve_image_file), а не
    береться з колонки наосліп: saved_path сьогодні наш, але відновлення з
    бекапу може покласти туди будь-що. Прибране зображення й зниклий файл
    дають однакові 404 — один деградований стан, не два.
    """
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    image = db.get(ShiftNoteImage, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="зображення не знайдено")

    path = resolve_image_file(image)
    media_type = media_type_for(path) if path is not None else None
    if path is None or media_type is None:
        raise HTTPException(status_code=404, detail="зображення не знайдено")

    return FileResponse(
        path,
        media_type=media_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/shift/images/{image_id}/delete")
def delete_shift_image(request: Request, image_id: int, db: Session = Depends(get_db)):
    """Прибрати помилково вставлений скріншот. Видаляє лише автор записки."""
    user = _require_user(request, db)
    image = db.get(ShiftNoteImage, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="зображення не знайдено")
    if image.note.author_id != user.id:
        return toast_response("Видаляти може лише автор записки.", kind="error")

    delete_image(db, image)
    db.commit()
    return toast_response("Скріншот видалено.", triggers={"refresh-shift": True})


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


@router.post("/shift/images/prune")
def prune_shift_images_now(request: Request, db: Session = Depends(get_db)):
    """«Прибрати зараз» — те саме, що робить добовий фоновий тік.

    Список перераховується всередині prune_shift_images, а не береться зі
    звіту на сторінці: між рендером і натисканням могло минути скільки
    завгодно. Текст записок не чіпається — лишається мітка «зображення
    прибрано».
    """
    user = _require_user(request, db)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    removed, freed = prune_shift_images(db)
    db.commit()
    if not removed:
        return toast_response("Прибирати нічого.", kind="info")
    return toast_response(
        f"Прибрано {removed} файл(ів), звільнено {round(freed / (1024 * 1024), 1)} МБ.",
        triggers={"refresh-shift": True},
    )
