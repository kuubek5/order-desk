"""Сповіщення: стан для клієнта і збереження налаштувань під оператора.

Це налаштування ОПЕРАТОРА, не машини, — тому обидва роути свідомо без
адмінського гейта (виняток зафіксовано в tests/test_admin_gate.py).
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.mail_inbox import in_inbox
from app.models import EmailMessage, Order
from app.routers.deps import (
    get_current_user,
    get_db,
    is_loopback_request,
    queue_can_take_ids,
    toast_response,
)
from app.services import desktop_popup
from app.services.shift import open_note_count as open_shift_note_count
from app.settings_store import set_notify_prefs
from app.sync_heartbeat import sync_status_pair
from app.update_check import get_known_update

router = APIRouter()


@router.get("/api/notify-state")
def api_notify_state(request: Request, db: Session = Depends(get_db)):
    """Cheap snapshot the client polls to detect system events worth a popup.

    Deliberately NOT a push channel: the browser compares this against its own
    previous snapshot and raises a toast on a TRANSITION (ok → error, count
    grew). That keeps the trigger logic in one place client-side and means a
    missed poll can never replay an old alert — the next poll just reflects
    reality. Everything here is already computed for the queue page, so this
    costs two scalar counts and two in-memory heartbeat reads.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    # Присутність браузера ЦЬОГО ПК для власного вікна-сповіщення (05.10.26):
    # яка сторінка відкрита й чи вікно у фокусі. Лише з петлі — вікно
    # малюється на цьому ПК, і браузер колеги з мережі тут нічого не вирішує.
    q = getattr(request, "query_params", None) or {}
    if q.get("page") is not None and is_loopback_request(request):
        desktop_popup.note_presence(
            page=q.get("page", ""), visible=q.get("vis") == "1",
            focused=q.get("focus") == "1", origin=q.get("origin", "")[:100],
        )

    status = sync_status_pair(db, datetime.now())
    release = get_known_update()
    return {
        "sheet": status["sheet"]["state"],
        "mail": status["mail"]["state"],
        "sheet_label": status["sheet"]["label"],
        "mail_label": status["mail"]["label"],
        "orders": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status != "видано", Order.archived_at.is_(None))
        ) or 0,
        # Та сама умова, що вкладка «Вхідні» (app/mail_inbox.py).
        "mail_pending": db.scalar(
            select(func.count()).select_from(EmailMessage).where(in_inbox)
        ) or 0,
        # Works a technician corrected in the sheet and nobody has acknowledged
        # yet. A rise means a fresh correction — the client toasts on that, so
        # the operator learns about it even while looking at the machines.
        "changed": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.sheet_changed_at.is_not(None), Order.archived_at.is_(None))
        ) or 0,
        # Відкриті записки передачі зміни — приріст означає, що колега
        # щойно щось передав. Той самий предикат, що й дошка/бейдж
        # (app/services/shift.py), щоб три місця не розходились.
        "shift": open_shift_note_count(db),
        # «Можна брати»: технік доклав шлях до папки, оператор ще не взяв.
        # Це СТАН готовності (§5), а не розмір черги — тому приріст означає
        # рівно «зʼявилась робота, яку можна брати». Саме через це старий
        # `new_orders` (розмір черги) не годився і був прибраний.
        "ready": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.job_code.is_not(None),
                Order.job_code != "",
                or_(Order.sum3d_id.is_(None), Order.sum3d_id == ""),
                Order.status != "видано",
                Order.archived_at.is_(None),
            )
        ) or 0,
        # Рядки, що зникли з таблиці. `archived_at` штампує лише синк, коли
        # рядок не знайшовся (sync.py), і видалення з черги — retention його
        # НЕ чіпає. Приріст = рядок прибрали, а не «робота постаріла».
        "deleted": db.scalar(
            select(func.count()).select_from(Order).where(Order.archived_at.is_not(None))
        ) or 0,
        "update": release.version if release else None,
        # Бейдж «Черга» в рейці НАЖИВО (власник 05.10.26: «на сторінці пошти не
        # бачу, що зʼявилась робота в лабораторії»). Ті самі id, що рахує
        # бейдж (`queue_can_take_ids`, сьогоднішня лабораторія «можна брати»),
        # тож число в рейці, тост і чіп черги не розходяться. Список — щоб
        # браузер позначив «+N нових», яких оператор ще не бачив.
        "can_take_ids": queue_can_take_ids(),
    }


@router.post("/settings/notifications")
async def save_notification_prefs(request: Request, db: Session = Depends(get_db)):
    """Save popup look, placement and which system triggers may fire one.

    Operator-facing (not admin-only): these are per-installation display
    preferences on a single-workstation app, not a security boundary — the same
    reasoning that makes the folder paths operator-editable.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    form = await request.form()
    set_notify_prefs(
        db,
        style=(form.get("notify_style") or "").strip(),
        position=(form.get("notify_position") or "").strip(),
        events=form.getlist("notify_events"),
    )
    db.commit()
    if request.headers.get("HX-Request") == "true":
        return toast_response("Налаштування сповіщень збережено")
    # Без JS повертаємо туди, ЗВІДКИ прийшли: розділ переїхав у кабінет
    # (/account, вкладка «Сповіщення»), і старий редірект на /settings кидав
    # би людину на «Стан системи» — секції з таким якорем там більше немає.
    return RedirectResponse("/account#notifications", status_code=303)


def _popup_gate(request: Request, db: Session):
    """Вікно-сповіщення налаштовується лише з ПК, де встановлено KuubMill:
    воно малюється на ЦЬОМУ екрані. Не адмінське — це місце й час для людини
    за цим ПК, як і решта «Сповіщень»."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Налаштовується на ПК, де встановлено KuubMill")


@router.post("/settings/desktop-popup")
async def save_desktop_popup(request: Request, db: Session = Depends(get_db)):
    """Зберегти налаштування вікна. Блок у кабінеті шле ВЕСЬ свій стан щоразу
    (FormData), тож відсутня галочка тут справді означає «знято»."""
    _popup_gate(request, db)
    form = await request.form()
    try:
        monitor = int(str(form.get("monitor") or 0))
        seconds = 0 if form.get("until_click") else int(str(form.get("seconds") or 8))
    except ValueError:
        raise HTTPException(status_code=400, detail="некоректне число")
    desktop_popup.save_settings(
        db,
        enabled=bool(form.get("enabled")),
        events={str(v) for v in form.getlist("events")},
        anchor=str(form.get("anchor") or "br"),
        monitor=monitor,
        seconds=seconds,
    )
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/settings/desktop-popup/test")
async def test_desktop_popup(request: Request, db: Session = Depends(get_db)):
    """Пробне вікно — одразу, без правил присутності: подивитись вигляд і місце."""
    _popup_gate(request, db)
    if not desktop_popup.supported():
        raise HTTPException(status_code=409, detail="Вікно показує програма KuubMill на Windows")
    form = await request.form()
    desktop_popup.get_popup_ui().test("mail" if form.get("kind") == "mail" else "lab")
    return JSONResponse({"ok": True})


@router.post("/settings/desktop-popup/place")
async def place_desktop_popup(request: Request, db: Session = Depends(get_db)):
    """Режим «Налаштувати положення»: вікно-зразок, яке тягнуть мишею."""
    _popup_gate(request, db)
    if not desktop_popup.supported():
        raise HTTPException(status_code=409, detail="Вікно показує програма KuubMill на Windows")
    desktop_popup.get_popup_ui().place()
    return JSONResponse({"ok": True})

