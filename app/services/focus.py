"""Робочий набір оператора — «мої зараз».

Оператор бере кілька нарядів у роботу й має не загубити, КУДИ вписувати
Sum3D ID. На папері це маркер; тут — персональна мітка на рядку плюс фільтр
«лише мої».

Модуль без Request/Response і без `db.commit()`: транзакцією володіє роут —
так само, як `log_action` (app/services/undo.py).
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, OrderFocus, User


def toggle(db: Session, order: Order, user: User, *, now: datetime | None = None) -> bool:
    """Поставити/зняти мітку. Повертає стан ПІСЛЯ дії (True = відмічено).

    Мітка персональна, тому обидві дії дивляться лише на пару
    (ця робота, цей оператор) — чужу мітку на тій самій роботі не чіпає.
    """
    row = db.scalar(
        select(OrderFocus).where(
            OrderFocus.order_id == order.id, OrderFocus.user_id == user.id
        )
    )
    if row is not None:
        db.delete(row)
        return False

    db.add(OrderFocus(order_id=order.id, user_id=user.id, created_at=now or datetime.now()))
    return True


def focused_ids(db: Session, user: User | None) -> set[int]:
    """Множина id робіт у наборі оператора — ОДНИМ запитом.

    Шаблон рядка питає `order.id in focused_ids`. Будь-який запит усередині
    циклу рядків тут був би регресією: черга рендерить 500+ рядків за раз.
    """
    if user is None:
        return set()
    return set(
        db.execute(select(OrderFocus.order_id).where(OrderFocus.user_id == user.id))
        .scalars()
        .all()
    )


def ranks(db: Session, user: User | None) -> dict[int, int]:
    """order_id → місце в наборі, за ЧАСОМ ПРИШПИЛЕННЯ (раніші попереду).

    Порядок усередині набору мусить бути один і той самий на сервері й на
    клієнті, інакше пришпилені перемішуються між собою: клієнт клав щойно
    пришпилену роботу вгору набору, сервер через кілька секунд повертав її на
    місце за порядком черги — і рядки стрибали під рукою.

    Обрано саме час пришпилення за зростанням: нова мітка ДОДАЄТЬСЯ В КІНЕЦЬ
    набору, тому жоден уже пришпилений рядок не рухається взагалі. Набір
    росте вниз, як список, який оператор складає руками.

    Одним запитом — черга рендерить 500+ рядків, запит у циклі був би
    регресією (та сама причина, що у focused_ids).
    """
    if user is None:
        return {}
    rows = db.execute(
        select(OrderFocus.order_id)
        .where(OrderFocus.user_id == user.id)
        # id як другий ключ: created_at у двох міток може збігтись до
        # мікросекунди (подвійний клік, дві вкладки), і без нього порядок
        # ставав би недетермінованим саме там, де він і потрібен.
        .order_by(OrderFocus.created_at.asc(), OrderFocus.id.asc())
    ).scalars().all()
    return {order_id: place for place, order_id in enumerate(rows)}


def count(db: Session, user: User | None) -> int:
    """Скільки робіт у наборі — для лічильника у фільтрі."""
    if user is None:
        return 0
    return int(
        db.execute(
            select(func.count(OrderFocus.id)).where(OrderFocus.user_id == user.id)
        ).scalar_one()
    )


def clear_all(db: Session, user: User) -> int:
    """Зняти всі мітки оператора; повертає, скільки знято.

    Роут питає підтвердження (рішення власника 29.08.26): набір із двох
    десятків рядків збирається руками, випадковий клік коштує заходу.
    """
    rows = list(
        db.execute(select(OrderFocus).where(OrderFocus.user_id == user.id)).scalars()
    )
    for row in rows:
        db.delete(row)
    return len(rows)


def release(db: Session, order: Order, user: User | None) -> None:
    """Зняти мітку, бо причина зникла — Sum3D ID вписано.

    Викликається в тій самій транзакції, що й запис Sum3D. Мітка існує рівно
    для того, щоб не загубити, куди його вписувати; далі вона тільки шумить.
    Знімається мітка ТОГО, ХТО вписав: якщо роботу тримав у наборі й колега,
    його мітка лишається — це його набір, не наш.
    """
    if user is None:
        return
    row = db.scalar(
        select(OrderFocus).where(
            OrderFocus.order_id == order.id, OrderFocus.user_id == user.id
        )
    )
    if row is not None:
        db.delete(row)
