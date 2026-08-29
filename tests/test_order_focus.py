"""Робочий набір «мої зараз»: персональність, ідемпотентність, автозняття.

Перевіряються рівно ті властивості, через які мітку варто було робити
таблицею, а не колонкою: вона належить операторові, а не роботі.
"""

import datetime as _dt
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.routers import orders as orders_router
from app.routers import queue as queue_router
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
    order = Order(source="lab", sheet_tab=_dt.date.today().strftime("%d.%m.%y"), row_number=7, work_order_no=no, status="нове")
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


# ── Інтерфейс і дві тихі пастки ──────────────────────────────────────────


def _request(user_id=None):
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id}, query_params={}, headers={}
    )


def test_toggle_route_returns_the_row_and_401s_without_a_session():
    with Session(_database()) as db:
        user, order = _user(db), _order(db)

        html = orders_router.toggle_order_focus(
            request=_request(user.id), order_id=order.id, db=db
        ).body.decode("utf-8")
        assert 'class="queue-row queue-row-focus' in html
        assert 'aria-pressed="true"' in html
        assert focused_ids(db, user) == {order.id}

        html = orders_router.toggle_order_focus(
            request=_request(user.id), order_id=order.id, db=db
        ).body.decode("utf-8")
        assert "queue-row-focus" not in html
        assert 'aria-pressed="false"' in html

        with pytest.raises(HTTPException) as exc:
            orders_router.toggle_order_focus(request=_request(None), order_id=order.id, db=db)
        assert exc.value.status_code == 401


def test_poll_branch_sees_the_same_marks_as_the_page(monkeypatch):
    """Пастка: гілка partial=rows мусить рахувати набір так само, як повна
    сторінка. Розійдись вони — мітки зникали б кожні 15 секунд, і це читалось
    би як «система забула», а не як пропущений ключ контексту."""
    captured = {}
    monkeypatch.setattr(
        web.templates,
        "TemplateResponse",
        lambda request, template, context: captured.setdefault(template, context) or context,
    )
    with Session(_database()) as db:
        user, order = _user(db), _order(db)
        toggle(db, order, user)
        db.commit()

        page = queue_router.get_queue(request=_request(user.id), period="earlier", db=db)
        captured.clear()
        rows = queue_router.get_queue(
            request=_request(user.id), period="earlier", partial="rows", db=db
        )

    assert page["focused_ids"] == rows["focused_ids"] == {order.id}
    assert page["focus_count"] == rows["focus_count"] == 1


def test_mine_filter_hides_everything_else_and_rides_the_poll_query(monkeypatch):
    """Пастка: `mine=1` мусить потрапити в rows_qs. Інакше перший же тік полла
    перезапросить НЕвідфільтрований вигляд і поверне приховані рядки."""
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(_database()) as db:
        user = _user(db)
        mine = _order(db, no="24122")
        _order(db, no="24999")
        toggle(db, mine, user)
        db.commit()

        # sheet_tab у хелпері — сьогоднішній день, тож роботи в бакеті «today».
        wide = queue_router.get_queue(request=_request(user.id), db=db)
        narrow = queue_router.get_queue(request=_request(user.id), mine="1", db=db)

    assert len(wide["orders"]) == 2
    assert [o.id for o in narrow["orders"]] == [mine.id]
    assert "mine=1" in narrow["rows_qs"]
    assert "mine" not in wide["rows_qs"]
    assert narrow["focus_mine"] is True and wide["focus_mine"] is False


def test_every_row_render_passes_focused_ids():
    """Сторож ключа контексту: рядок може намалювати ПЕРСОНАЛЬНУ мітку лише
    знаючи, хто дивиться. Кожен рендер _order_row.html іде через _row_context —
    прямий словник тут означає рядок без мітки після дії."""
    source = Path("app/routers/orders.py").read_text(encoding="utf-8")
    assert '"_order_row.html", {' not in source, (
        "рендер рядка з прямим словником замість _row_context — мітка «мої зараз» зникне"
    )


def test_clear_all_route_is_personal_and_reports_nothing_to_clear():
    with Session(_database()) as db:
        user, other = _user(db, "a"), _user(db, "b")
        order = _order(db)
        toggle(db, order, user)
        toggle(db, order, other)
        db.commit()

        response = orders_router.clear_order_focus(request=_request(user.id), db=db)
        assert response.status_code == 204
        assert focused_ids(db, user) == set()
        assert focused_ids(db, other) == {order.id}, "чужий набір не чіпаємо"

        again = orders_router.clear_order_focus(request=_request(user.id), db=db)
        assert b"" == again.body
