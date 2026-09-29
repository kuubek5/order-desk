"""Дзеркало черги внизу екрана пошти: read-only список робіт, ПРИЙНЯТИХ саме
з пошти (source == "email"). Оператор прийняв лист — робота одразу видима внизу,
без переходу в чергу. НЕ вся черга (рішення власника 23.09.26): лабораторні й
табличні клієнтські рядки сюди не йдуть.

Свій легкий прохід, а не build_queue_view: тут не треба ні фільтрів періоду, ні
шпильок «мої зараз», ні сканування мережевих тек — лише кілька полів на
компактний рядок.

Скидається щодня (власник 29.09.26): раніше список тримав 30-денне вікно
retention, як черга, і робота лишалась видимою тут навіть ПІСЛЯ видачі —
Юрія Бойка прийняли з пошти 25.09, видали 28.09, а рядок все одно висів
29.09 серед щойно прийнятих листів, до яких оператор більше не мав стосунку.
Дзеркало — не «черга для email», а «що я оприйняв сьогодні»; вчорашнє
(зроблене чи ні) належить самій черзі, не цьому списку.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.business_day import business_tab_today
from app.models import Order
from app.services.order_dates import order_date


def mail_mirror_orders(db: Session) -> list[Order]:
    """Активні роботи з пошти, прийняті СЬОГОДНІ (робоча вкладка дня;
    у вихідні — п'ятнична, `business_tab_today`, той самий годинник, що в
    черги й видачі), найновіша згори.

    `archived_at IS NULL` стоїть у SQL (архів росте щомісяця); відсів за
    днем — у Python, бо бізнес-дата виводиться з `sheet_tab`, а не зі стовпця
    (як у build_queue_view). `Order.material` тягнемо одразу — його читає
    material_badge для чипа матеріалу в рядку дзеркала.
    """
    today = business_tab_today()
    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.material))
        .where(Order.source == "email", Order.archived_at.is_(None))
        .order_by(Order.id.desc())
    ).all()
    orders = [o for o in orders if order_date(o) == today]
    # Порядок СТВОРЕННЯ, найновіша згори (`Order.id DESC` уже в SQL; власник
    # 25.09.26: «остання робота завжди зверху»). Не порядок таблиці: рядки там
    # зсуваються від видалень і дописувань (на dev 25.09.26 робота, прийнята
    # о 12:26, стояла в рядку нижче за прийняту о 13:59), і дзеркало за рядком
    # ставило б свіжу роботу посередині.
    return orders
