"""Архів робіт, концепт «Хроніка»: місяці → календар днів → роботи дня.

Показує все, що викотилось із робочої черги (старше за RETENTION_DAYS або
явно заархівоване, бо зникло з Google). Деталь — той самий паспорт
/orders/{id}, тому стара робота лишається повністю відновлюваною, а не просто
переліченою.

Розкладка — майстер-деталь (обрано власником, 05.09.26): місяці ліворуч
завжди на очах, а вибір місяця/дня НЕ перемикає сторінку — праву панель
підмінює HTMX. Так контекст (де ти в архіві) не губиться між кліками.
Повна сторінка `/archive` віддає оболонку з уже розкритим найсвіжішим
місяцем; далі — три HTMX-партіали:
  • GET /archive/detail  — календар одного місяця у праву панель;
  • GET /archive/day     — роботи дня у нижній слот правої панелі;
  • GET /archive/search  — наскрізний пошук по архіву у праву панель.
"""

import calendar
from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from app.business_day import business_today
from app.models import Order
from app.routers.deps import get_current_user, login_redirect, get_db, templates
from app.services.formatting import uk_month_label
from app.services.order_dates import order_date, parse_sheet_tab
from app.services.queue import RETENTION_DAYS, order_is_archived

router = APIRouter()

# Скільки рядків максимум віддає наскрізний пошук — щоб «титан» не витяг весь
# архів у праву панель одним запитом.
SEARCH_LIMIT = 200


def parse_archive_month(value: str) -> tuple[int, int] | None:
    """Parse an Archive month param 'YYYY-MM' into (year, month), or None."""
    try:
        year_s, month_s = value.split("-", 1)
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        return None
    if 1 <= month <= 12 and 2000 <= year <= 2100:
        return year, month
    return None


def _load_archived(db: Session) -> tuple[list[Order], dict, dict]:
    """Всі заarchived-роботи + подобові й помісячні лічильники (один прохід).

    Межа архіву — від `business_today()`, ТА САМА, що в черзі (queue.py). З
    календарною добою вони розходились би щоночі до межі зміни — рівно коли
    нічний оператор працює (тому саме business_today, не календарний today).
    """
    today = business_today()
    cutoff = today - timedelta(days=RETENTION_DAYS)
    all_orders = db.scalars(select(Order).options(selectinload(Order.material))).all()
    archived = [o for o in all_orders if order_is_archived(o, cutoff)]

    day_counts: dict[tuple[int, int, int], int] = defaultdict(int)
    month_counts: dict[tuple[int, int], int] = defaultdict(int)
    for order in archived:
        d = order_date(order)
        day_counts[(d.year, d.month, d.day)] += 1
        month_counts[(d.year, d.month)] += 1
    return archived, day_counts, month_counts


def _months_rail(day_counts: dict, month_counts: dict) -> list[dict]:
    """Список місяців для лівої рейки (новіші згори), кожен зі спарклайном."""
    months = []
    for (year, mon), cnt in sorted(month_counts.items(), reverse=True):
        weeks = calendar.monthcalendar(year, mon)
        spark = [
            sum(day_counts.get((year, mon, dn), 0) for dn in week if dn)
            for week in weeks
        ]
        months.append(
            {
                "ym": f"{year:04d}-{mon:02d}",
                "label": uk_month_label(year, mon),
                "count": cnt,
                "spark": spark,
                "spark_max": max(spark, default=1) or 1,
            }
        )
    return months


def _month_detail_ctx(year: int, mon: int, day_counts: dict, month_counts: dict) -> dict:
    """Контекст правої панелі для одного місяця (календар-heatmap)."""
    weeks = calendar.monthcalendar(year, mon)
    grid = [
        [
            (
                {
                    "day": dn,
                    "count": day_counts.get((year, mon, dn), 0),
                    "date": date(year, mon, dn).strftime("%d.%m.%y"),
                }
                if dn
                else None
            )
            for dn in week
        ]
        for week in weeks
    ]
    month_max = max(
        (day_counts.get((year, mon, dn), 0) for week in weeks for dn in week if dn),
        default=0,
    )
    return {
        "month_ym": f"{year:04d}-{mon:02d}",
        "month_label": uk_month_label(year, mon),
        "month_grid": grid,
        "month_max": month_max,
        "month_total": month_counts.get((year, mon), 0),
    }


def _day_orders(archived: list[Order], selected_date: date) -> list[Order]:
    """Роботи дня В ПОРЯДКУ таблиці (за row_number) — «зазирнути в історію»:
    першочерговість рядків збережена так само, як у Google Таблиці. Рядки без
    row_number (пошта) ідуть у кінець, стабільно за id."""
    return sorted(
        (o for o in archived if order_date(o) == selected_date),
        key=lambda o: (o.row_number is None, o.row_number or 0, o.id),
    )


# Порожня відповідь на невалідний параметр показувала порожнє місце: HTMX не
# свапає 4xx узагалі, тож оператор бачив, що «нічого не сталось», і тиснув ще
# раз. Тепер це 200 із поясненням — стан «зрозуміли запит, показати нема чого»
# (аудит 05.09.26, UX 1.9).
def _archive_notice(text: str) -> HTMLResponse:
    return HTMLResponse(f'<div class="arch-empty" role="status">{text}</div>')


@router.get("/archive", response_class=HTMLResponse)
def get_archive(request: Request, month: str = "", date: str = "", db: Session = Depends(get_db)):
    """Оболонка архіву: ліва рейка місяців + права панель з уже розкритим
    місяцем. Далі рівні підмінює HTMX, сторінка не перезавантажується.

    `month`/`date` в адресі — те, що записує `hx-push-url`: перезавантаження
    сторінки, «назад» у браузері й надіслане колезі посилання відкривають той
    самий місяць і день, а не найсвіжіший (аудит 05.09.26, UX 1.9)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    archived, day_counts, month_counts = _load_archived(db)
    months = _months_rail(day_counts, month_counts)

    base = {
        "page_title": "Архів",
        "user": user,
        "archive_total": len(archived),
        "months": months,
    }
    if months:
        known = {m["ym"] for m in months}
        wanted = month if month in known else months[0]["ym"]
        year, mon = parse_archive_month(wanted)
        base.update(_month_detail_ctx(year, mon, day_counts, month_counts))
        base["active_ym"] = wanted
        selected_date = parse_sheet_tab(date) if date else None
        if selected_date is not None:
            base["selected_date"] = selected_date
            base["day_label"] = selected_date.strftime("%d.%m.%Y")
            base["day_orders"] = _day_orders(archived, selected_date)
    return templates.TemplateResponse(request, "archive.html", base)


@router.get("/archive/detail", response_class=HTMLResponse)
def get_archive_detail(
    request: Request,
    month: str = "",
    db: Session = Depends(get_db),
):
    """HTMX-партіал: календар одного місяця у праву панель (#arch-detail)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    parsed = parse_archive_month(month)
    if parsed is None:
        return _archive_notice("Місяць не розпізнано — оберіть його в списку ліворуч.")
    _, day_counts, month_counts = _load_archived(db)
    year, mon = parsed
    ctx = {"user": user, **_month_detail_ctx(year, mon, day_counts, month_counts)}
    return templates.TemplateResponse(request, "_arch_detail.html", ctx)


@router.get("/archive/day", response_class=HTMLResponse)
def get_archive_day(
    request: Request,
    date_param: Annotated[str, Query(alias="date")] = "",
    db: Session = Depends(get_db),
):
    """HTMX-партіал: роботи одного дня у нижній слот правої панелі (#arch-day)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    selected_date = parse_sheet_tab(date_param) if date_param else None
    if selected_date is None:
        return _archive_notice("Дату не розпізнано — оберіть день у календарі.")
    archived, _, _ = _load_archived(db)
    ctx = {
        "user": user,
        "selected_date": selected_date,
        "day_label": selected_date.strftime("%d.%m.%Y"),
        "day_orders": _day_orders(archived, selected_date),
    }
    return templates.TemplateResponse(request, "_arch_daylist.html", ctx)


@router.get("/archive/search", response_class=HTMLResponse)
def get_archive_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
):
    """HTMX-партіал: наскрізний пошук по архіву у праву панель.

    Порожній запит повертає найсвіжіший місяць — так очищення поля повертає
    операторa туди, де він був. Шукає по № наряду, клієнту, Sum3D ID,
    матеріалу й виду роботи (регістронезалежно, підрядок)."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    archived, day_counts, month_counts = _load_archived(db)
    needle = q.strip().lower()

    if not needle:
        months = _months_rail(day_counts, month_counts)
        if not months:
            return templates.TemplateResponse(
                request, "_arch_detail.html", {"user": user, "month_grid": []}
            )
        year, mon = parse_archive_month(months[0]["ym"])
        ctx = {"user": user, **_month_detail_ctx(year, mon, day_counts, month_counts)}
        return templates.TemplateResponse(request, "_arch_detail.html", ctx)

    def hit(o: Order) -> bool:
        fields = (
            o.work_order_no,
            o.client_name,
            o.sum3d_id,
            o.material_color,
            o.kind,
        )
        return any(f and needle in str(f).lower() for f in fields)

    matches = sorted(
        (o for o in archived if hit(o)),
        key=lambda o: (order_date(o), o.work_order_no or o.client_name or ""),
        reverse=True,
    )
    truncated = len(matches) > SEARCH_LIMIT
    results = [
        {"order": o, "date_label": order_date(o).strftime("%d.%m.%y")}
        for o in matches[:SEARCH_LIMIT]
    ]
    return templates.TemplateResponse(
        request,
        "_arch_search.html",
        {
            "user": user,
            "query": q.strip(),
            "results": results,
            "result_total": len(matches),
            "truncated": truncated,
        },
    )
