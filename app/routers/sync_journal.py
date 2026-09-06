"""Екран «Журнал синку» — що саме робив синк і чим це закінчилось.

Навіщо окремий екран. `SyncLog` пишеться давно й рясно (кожен запис у
таблицю, кожна помилка читання, кожен пропущений рядок), але побачити його
було ніде: результат долітав до оператора лише разовим тостом, який зникав
разом зі свапом HTMX. Тобто «вчора запис у таблицю не пройшов» дізнатись було
неможливо взагалі — а це рівно той клас збоїв, який мовчить (аудит 05.09.26,
крок 3.1).

Гейт — адмін. Це діагностика, а не робочий інструмент оператора: рядки
технічні (`order 812: sum3d_id`), і читати їх має той, хто вміє щось із ними
зробити.

Шлях `/journal/sync`, а не `/sync/journal`: журнал дій уже живе на `/journal`,
і це друга книга тієї самої полиці — так вона знаходиться там, де людина вже
звикла шукати «історію». Обидва шляхи статичні, тож жодного конфлікту з
`{param}`-роутами (пастка `/settings/furnaces/password`, CLAUDE.md §14) тут
немає.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import SyncLog
from app.routers.deps import get_current_user, get_db, login_redirect, templates
from app.services.queue_view import live_sync_status

router = APIRouter()

# Людські назви напрямків. Значення — рівно ті рядки, які пишуть
# sheet_sync_service / mail_sync_service / sheet_writeback / mail_accept;
# новий напрямок без рядка тут покажеться як є, а не зникне з фільтра.
DIRECTION_LABELS: dict[str, str] = {
    "sheet_to_db": "таблиця → CRM",
    "db_to_sheet": "CRM → таблиця",
    "mail_to_db": "пошта → CRM",
    "mail_to_export": "пошта → export",
    "mail_to_sheet": "пошта → таблиця",
}

STATUS_LABELS: dict[str, str] = {
    "ok": "успіх",
    # «Пропущено» — НЕ успіх: рядок свідомо не записано (пауза синку, рядок у
    # таблиці не підтверджено). Саме цей стан раніше йшов у журнал як `ok`.
    "skipped": "пропущено",
    "error": "помилка",
}

# Скільки рядків показуємо за один раз. Журнал росте швидко (кожен запис у
# таблицю — рядок), тож вікно жорстке, а «є ще» чесно написано на екрані.
PAGE_LIMIT = 400


@router.get("/journal/sync", response_class=HTMLResponse)
def get_sync_journal(
    request: Request,
    direction: str = "",
    status: str = "",
    day: str = "",
    db: Session = Depends(get_db),
):
    """Стрічка `SyncLog`, згорнута по днях, з фільтром напрямку/статусу/дня."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    query = select(SyncLog).order_by(SyncLog.occurred_at.desc())
    # Фільтруємо лише за ВІДОМИМИ значеннями: підроблений параметр не має
    # давати порожній екран, який читається як «синк мовчить».
    selected_direction = direction if direction in DIRECTION_LABELS else ""
    if selected_direction:
        query = query.where(SyncLog.direction == selected_direction)
    selected_status = status if status in STATUS_LABELS else ""
    if selected_status:
        query = query.where(SyncLog.status == selected_status)

    selected_day = ""
    if day:
        try:
            picked = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            pass
        else:
            query = query.where(
                SyncLog.occurred_at >= picked,
                SyncLog.occurred_at < picked + timedelta(days=1),
            )
            selected_day = day

    entries = db.execute(query.limit(PAGE_LIMIT + 1)).scalars().all()
    truncated = len(entries) > PAGE_LIMIT
    entries = entries[:PAGE_LIMIT]

    # Групування по днях робить сервер, а не шаблон: у Jinja це вийшов би
    # цикл із памʼяттю про попередній рядок, який мовчки ламається на
    # першому ж `occurred_at is None` (у старих рядків серверний default міг
    # не спрацювати).
    groups: list[dict] = []
    for entry in entries:
        key = entry.occurred_at.date() if entry.occurred_at else None
        if not groups or groups[-1]["day"] != key:
            groups.append({"day": key, "rows": []})
        groups[-1]["rows"].append(entry)

    counts = {"ok": 0, "skipped": 0, "error": 0}
    for entry in entries:
        if entry.status in counts:
            counts[entry.status] += 1

    return templates.TemplateResponse(
        request,
        "sync_journal.html",
        {
            "user": user,
            "groups": groups,
            "counts": counts,
            "truncated": truncated,
            "limit": PAGE_LIMIT,
            "direction_labels": DIRECTION_LABELS,
            "status_labels": STATUS_LABELS,
            "selected_direction": selected_direction,
            "selected_status": selected_status,
            "selected_day": selected_day,
            # Живий стан пари «таблиця / пошта» — той самий, що малює кружок у
            # рейці. Тут він на місці: сторінка й існує заради питання «чи
            # синк узагалі живий».
            "sync_status": live_sync_status(db),
        },
    )
