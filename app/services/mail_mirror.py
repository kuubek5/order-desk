"""Дзеркало черги внизу екрана пошти: read-only список робіт, ПРИЙНЯТИХ саме
з пошти (source == "email"). Оператор прийняв лист — робота одразу видима внизу,
без переходу в чергу. НЕ вся черга (рішення власника 23.09.26): лабораторні й
табличні клієнтські рядки сюди не йдуть.

Свій легкий прохід, а не build_queue_view: тут не треба ні фільтрів періоду, ні
шпильок «мої зараз», ні сканування мережевих тек — лише кілька полів на
компактний рядок. Той самий відсів, що й у черги (active + вікно retention), щоб
дзеркало не показувало того, чого в черзі вже немає.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.business_day import business_today
from app.models import Order
from app.services.order_dates import order_date
from app.services.queue import RETENTION_DAYS


def mail_mirror_orders(db: Session) -> list[Order]:
    """Активні роботи з пошти в межах вікна retention, найновіша згори.

    `archived_at IS NULL` стоїть у SQL (архів росте щомісяця); відсів за вікном
    retention — у Python, бо бізнес-дата виводиться з `sheet_tab`, а не зі
    стовпця (як у build_queue_view). `Order.material` тягнемо одразу — його
    читає material_badge для чипа матеріалу в рядку дзеркала.
    """
    cutoff = business_today() - timedelta(days=RETENTION_DAYS)
    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.material))
        .where(Order.source == "email", Order.archived_at.is_(None))
        .order_by(Order.id.desc())
    ).all()
    orders = [o for o in orders if order_date(o) >= cutoff]
    # Порядок СТВОРЕННЯ, найновіша згори (`Order.id DESC` уже в SQL; власник
    # 25.09.26: «остання робота завжди зверху»). Не порядок таблиці: рядки там
    # зсуваються від видалень і дописувань (на dev 25.09.26 робота, прийнята
    # о 12:26, стояла в рядку нижче за прийняту о 13:59), і дзеркало за рядком
    # ставило б свіжу роботу посередині.
    return orders
