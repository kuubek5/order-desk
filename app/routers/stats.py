"""Екран статистики: одиниці/роботи за період, брак по винуватцях, середній
час «нове → відфрезеровано».

Один роут, який лише збирає період і віддає підрахунки з app/stats.py у
шаблон — рахує гроші не CRM, її справа чесно віддати цифри (CLAUDE.md §5).
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from app.models import Order, ReworkRecord
from app.routers.deps import get_current_user, login_redirect, get_db, templates
from app.services.order_dates import order_date
from app.stats import (
    average_new_to_milled_hours,
    parse_int_safe,
    summarize_by_material,
    summarize_rework_by_blame,
)

router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
def get_stats(request: Request, period: str = "week", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    if period not in ("today", "week", "month", "all"):
        period = "week"

    today = date.today()
    period_start = {
        "today": today,
        "week": today - timedelta(days=6),
        "month": today - timedelta(days=29),
        "all": None,
    }[period]

    all_orders = db.scalars(
        select(Order).options(
            selectinload(Order.status_events), selectinload(Order.material)
        )
    ).all()

    period_orders = []
    for order in all_orders:
        if period == "all":
            period_orders.append(order)
            continue
        if period_start <= order_date(order) <= today:
            period_orders.append(order)

    order_count = len(period_orders)
    quantity_sum = sum(
        qty for qty in (parse_int_safe(order.quantity) for order in period_orders) if qty is not None
    )

    rework_records = db.scalars(select(ReworkRecord)).all()
    rework_groups = summarize_rework_by_blame(rework_records)

    avg_hours = average_new_to_milled_hours(period_orders)
    material_groups = summarize_by_material(period_orders)

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "page_title": "Статистика",
            "user": user,
            "period": period,
            "order_count": order_count,
            "quantity_sum": quantity_sum,
            "rework_groups": rework_groups,
            "avg_hours": avg_hours,
            "material_groups": material_groups,
        },
    )
