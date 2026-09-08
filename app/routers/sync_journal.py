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

import json
import logging
from datetime import datetime, timedelta

from app.business_day import business_to_utc, utc_to_business

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models import SyncLog
from app.routers.deps import get_current_user, get_db, login_redirect, templates
from app.services.queue_view import live_sync_status
from app.sheet_writer import RowOccupiedError, restore_erased_row
from app.sheets import get_worksheet_by_name, open_spreadsheet

logger = logging.getLogger(__name__)

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
# Сторінка зменшена з 400 до 150: чотириста записів важили ~200 КБ, а дивляться
# зазвичай кілька останніх. «Показати ще» довантажує таку саму порцію
# (ревʼю 07.09.26, P.3).
PAGE_LIMIT = 150
PAGE_LIMIT_MAX = 2000


def clamp_journal_limit(limit) -> int:
    try:
        value = int(limit) if limit not in (None, "") else PAGE_LIMIT
    except (TypeError, ValueError):
        return PAGE_LIMIT
    return max(PAGE_LIMIT, min(value, PAGE_LIMIT_MAX))


@router.get("/journal/sync", response_class=HTMLResponse)
def get_sync_journal(
    request: Request,
    direction: str = "",
    status: str = "",
    day: str = "",
    restored: str = "",
    limit: str = "",
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
            # Межі дня — київські, а в базі `occurred_at` за Гринвічем: інакше
            # вечірні записи (після 21:00 UTC) випадали в «наступний день».
            query = query.where(
                SyncLog.occurred_at >= business_to_utc(picked),
                SyncLog.occurred_at < business_to_utc(picked + timedelta(days=1)),
            )
            selected_day = day

    page_limit = clamp_journal_limit(limit)
    entries = db.execute(query.limit(page_limit + 1)).scalars().all()
    truncated = len(entries) > page_limit
    entries = entries[:page_limit]

    # Групування по днях робить сервер, а не шаблон: у Jinja це вийшов би
    # цикл із памʼяттю про попередній рядок, який мовчки ламається на
    # першому ж `occurred_at is None` (у старих рядків серверний default міг
    # не спрацювати).
    groups: list[dict] = []
    for entry in entries:
        key = utc_to_business(entry.occurred_at).date() if entry.occurred_at else None
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
            "limit": page_limit,
            "next_limit": page_limit + PAGE_LIMIT,
            "direction_labels": DIRECTION_LABELS,
            "status_labels": STATUS_LABELS,
            "selected_direction": selected_direction,
            "selected_status": selected_status,
            "selected_day": selected_day,
            # Живий стан пари «таблиця / пошта» — той самий, що малює кружок у
            # рейці. Тут він на місці: сторінка й існує заради питання «чи
            # синк узагалі живий».
            "sync_status": live_sync_status(db),
            # Результат відновлення рядка після редиректу: банер угорі.
            "restore_result": restored,
        },
    )


@router.post("/journal/sync/{log_id}/restore-row")
def restore_erased_sheet_row(
    log_id: int, request: Request, db: Session = Depends(get_db)
):
    """Повернути в таблицю рядок, стертий при видаленні роботи.

    Звичайний `def`, а не `async`: усередині синхронний похід у Google Sheets,
    і на event loop йому не місце (CLAUDE.md §14) — FastAPI віднесе цей роут
    у threadpool сам.

    Відновлюємо ЗНАЧЕННЯ з журналу, а не поля роботи: роботи в базі вже нема,
    її й видалили. Рядок мусить бути порожній — інакше `restore_erased_row`
    відмовляється, бо лабораторія переюзує звільнені рядки.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    entry = db.get(SyncLog, log_id)
    if entry is None or not entry.erased_row or not entry.erased_values:
        return RedirectResponse("/journal/sync?restored=nothing", status_code=303)
    if entry.erased_restored_at is not None:
        return RedirectResponse("/journal/sync?restored=already", status_code=303)

    try:
        values = json.loads(entry.erased_values)
    except (TypeError, ValueError):
        return RedirectResponse("/journal/sync?restored=nothing", status_code=303)

    try:
        worksheet = get_worksheet_by_name(open_spreadsheet(db=db), entry.sheet_tab or "")
        if worksheet is None:
            raise RuntimeError(f"вкладки {entry.sheet_tab} немає в таблиці")
        restore_erased_row(worksheet, entry.erased_row, values)
    except RowOccupiedError:
        logger.warning("Відновлення рядка %s: рядок уже зайнято", entry.erased_row)
        return RedirectResponse("/journal/sync?restored=occupied", status_code=303)
    except Exception:
        logger.exception("Не вдалося відновити рядок за записом журналу %s", log_id)
        return RedirectResponse("/journal/sync?restored=error", status_code=303)

    entry.erased_restored_at = datetime.now()
    db.add(
        SyncLog(
            direction="db_to_sheet", sheet_tab=entry.sheet_tab, status="ok",
            message=(
                f"рядок {entry.erased_row} відновлено з журналу "
                f"({user.full_name or user.username})"
            ),
        )
    )
    db.commit()
    return RedirectResponse("/journal/sync?restored=ok", status_code=303)
