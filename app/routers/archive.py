"""Архів робіт, концепт «Хроніка»: місяці → календар днів → роботи дня.

Показує все, що викотилось із робочої черги (старше за RETENTION_DAYS або
явно заархівоване, бо зникло з Google). Деталь — той самий паспорт
/orders/{id}, тому стара робота лишається повністю відновлюваною, а не просто
переліченою.
"""

import calendar
from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from app.business_day import business_today
from app.models import Order
from app.routers.deps import get_current_user, get_db, templates
from app.services.formatting import uk_month_label
from app.services.order_dates import order_date, parse_sheet_tab
from app.services.queue import RETENTION_DAYS, order_is_archived

router = APIRouter()


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


@router.get("/archive", response_class=HTMLResponse)
def get_archive(
    request: Request,
    month: str = "",
    date_param: Annotated[str, Query(alias="date")] = "",
    db: Session = Depends(get_db),
):
    """Archive (Concept 1 «Хроніка») — everything that rolled out of the working
    queue: месяці → календар днів → роботи дня → повний паспорт. Drills down by
    time; the detail is the existing /orders/{id} passport (Sum3D ID, history,
    comments, rework) opened in the slide-over, so an old work is fully
    recoverable, not just listed."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Робоча доба, не календарна: черга рахує своє вікно від `business_today()`
    # (queue.py), і межа архіву мусить бути ТА САМА. З `date.today()` вони
    # розходились на добу щоночі з 00:00 до межі зміни — рівно в години, коли
    # нічний оператор і працює.
    today = business_today()
    cutoff = today - timedelta(days=RETENTION_DAYS)
    all_orders = db.scalars(select(Order).options(selectinload(Order.material))).all()
    archived = [o for o in all_orders if order_is_archived(o, cutoff)]

    # One pass: per-day and per-month tallies drive every level below.
    day_counts: dict[tuple[int, int, int], int] = defaultdict(int)
    month_counts: dict[tuple[int, int], int] = defaultdict(int)
    for order in archived:
        d = order_date(order)
        day_counts[(d.year, d.month, d.day)] += 1
        month_counts[(d.year, d.month)] += 1

    base = {"page_title": "Архів", "user": user, "archive_total": len(archived)}

    # ── Level 3: a single day's works ──────────────────────────────────────
    selected_date = parse_sheet_tab(date_param) if date_param else None
    if selected_date is not None:
        day_orders = sorted(
            (o for o in archived if order_date(o) == selected_date),
            key=lambda o: (o.work_order_no or o.client_name or "", o.id),
        )
        return templates.TemplateResponse(
            request,
            "archive.html",
            {
                **base,
                "level": "day",
                "selected_date": selected_date,
                "day_label": selected_date.strftime("%d.%m.%Y"),
                "back_month": f"{selected_date.year:04d}-{selected_date.month:02d}",
                "day_orders": day_orders,
            },
        )

    # ── Level 2: one month's calendar of days ──────────────────────────────
    parsed_month = parse_archive_month(month)
    if parsed_month is not None:
        year, mon = parsed_month
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
        return templates.TemplateResponse(
            request,
            "archive.html",
            {
                **base,
                "level": "month",
                "month_ym": f"{year:04d}-{mon:02d}",
                "month_label": uk_month_label(year, mon),
                "month_grid": grid,
                "month_max": month_max,
                "month_total": month_counts.get((year, mon), 0),
            },
        )

    # ── Level 1: months landing ────────────────────────────────────────────
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
    return templates.TemplateResponse(
        request, "archive.html", {**base, "level": "months", "months": months}
    )
