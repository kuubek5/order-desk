"""Робочий набір «мої зараз»: персональність, ідемпотентність, автозняття.

Перевіряються рівно ті властивості, через які мітку варто було робити
таблицею, а не колонкою: вона належить операторові, а не роботі.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order, OrderFocus, User
from app.services.focus import clear_all, count, focused_ids, release, toggle


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, username="op"):
    user = User(username=username, password_hash="x", full_name=username, role="оператор")
    db.add(user)
    db.commit()
    return user


def _order(db, no="24122"):
    order = Order(source="lab", sheet_tab="29.08.26", row_number=7, work_order_no=no, status="нове")
    db.add(order)
    db.commit()
    return order


def test_toggle_marks_then_unmarks():
    with Session(_database()) as db:
        user, order = _user(db), _order(db)

        assert toggle(db, order, user) is True
        db.commit()
        assert focused_ids(db, user) == {order.id}

        assert toggle(db, order, user) is False
        db.commit()
        assert focused_ids(db, user) == set()


def test_mark_is_personal_and_two_operators_do_not_collide():
    """Головна причина окремої таблиці: набір належить операторові. Мітка A
    невидима для B і не заважає B відмітити ту саму роботу."""
    with Session(_database()) as db:
        first, second = _user(db, "a"), _user(db, "b")
        order = _order(db)

        toggle(db, order, first)
        db.commit()
        assert focused_ids(db, first) == {order.id}
        assert focused_ids(db, second) == set()

        toggle(db, order, second)
        db.commit()
        assert focused_ids(db, second) == {order.id}

        # Зняття своєї мітки не чіпає чужу.
        toggle(db, order, first)
        db.commit()
        assert focused_ids(db, first) == set()
        assert focused_ids(db, second) == {order.id}


def test_duplicate_pair_is_refused_by_the_database():
    """Сторож лічильника: два рядки на ту саму пару зробили б «мої зараз · 2»
    над однією роботою."""
    with Session(_database()) as db:
        user, order = _user(db), _order(db)
        db.add(OrderFocus(order_id=order.id, user_id=user.id, created_at=datetime.now()))
        db.commit()

        db.add(OrderFocus(order_id=order.id, user_id=user.id, created_at=datetime.now()))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_count_matches_the_marked_set():
    with Session(_database()) as db:
        user = _user(db)
        other = _user(db, "b")
        orders = [_order(db, no=str(24000 + i)) for i in range(3)]
        for order in orders[:2]:
            toggle(db, order, user)
        toggle(db, orders[2], other)
        db.commit()

        assert count(db, user) == 2 == len(focused_ids(db, user))
        assert count(db, other) == 1
        assert count(db, None) == 0 and focused_ids(db, None) == set()


def test_clear_all_touches_only_that_operator():
    with Session(_database()) as db:
        user, other = _user(db, "a"), _user(db, "b")
        orders = [_order(db, no=str(24000 + i)) for i in range(3)]
        for order in orders:
            toggle(db, order, user)
        toggle(db, orders[0], other)
        db.commit()

        assert clear_all(db, user) == 3
        db.commit()

        assert focused_ids(db, user) == set()
        assert focused_ids(db, other) == {orders[0].id}


def test_release_drops_only_the_writers_mark():
    """Sum3D вписано — мітка того, хто вписав, зникає (причина відпала).
    Мітка колеги лишається: це його набір."""
    with Session(_database()) as db:
        writer, colleague = _user(db, "a"), _user(db, "b")
        order = _order(db)
        toggle(db, order, writer)
        toggle(db, order, colleague)
        db.commit()

        release(db, order, writer)
        db.commit()

        assert focused_ids(db, writer) == set()
        assert focused_ids(db, colleague) == {order.id}


def test_release_is_safe_when_nothing_is_marked():
    with Session(_database()) as db:
        user, order = _user(db), _order(db)
        release(db, order, user)
        release(db, order, None)
        db.commit()
        assert count(db, user) == 0
