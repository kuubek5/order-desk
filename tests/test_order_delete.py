"""Видалення роботи з черги.

Головне рішення: видалення = АРХІВАЦІЯ, а не знищення рядка в БД. Інакше
губиться історія статусів і коментарі, а наступний синк спокійно імпортує
роботу назад із таблиці. Рядок у таблиці при цьому ОЧИЩАЄТЬСЯ, а не
видаляється — видалення зсунуло б усі роботи нижче (див. test_sync.py).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.routers import orders as orders_router_mod
from app.db import Base
from app.models import Order, StatusEvent, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    u = User(username="op", password_hash="unused", full_name="Оператор", role="оператор")
    db.add(u)
    db.commit()
    return u


def _request(user_id, headers=None):
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers(headers or {}),
    )


def _order(db: Session, **kw) -> Order:
    defaults = dict(source="lab", sheet_tab="25.08.26", row_number=7,
                    work_order_no="24122", status="нове")
    defaults.update(kw)
    o = Order(**defaults)
    db.add(o)
    db.commit()
    return o


def test_delete_archives_instead_of_destroying():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        with patch.object(orders_router_mod, "clear_sheet_row_background") as clear:
            asyncio.run(orders_router_mod.delete_order(request=_request(user.id), order_id=order.id, db=db))
        clear.assert_called_once_with("25.08.26", 7)

    with Session(engine, expire_on_commit=False) as db:
        kept = db.get(Order, order.id)
        assert kept is not None, "рядок у БД має лишитись — інакше зникне історія"
        assert kept.archived_at is not None


def test_delete_records_who_did_it():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        with patch.object(orders_router_mod, "clear_sheet_row_background"):
            asyncio.run(orders_router_mod.delete_order(request=_request(user.id), order_id=order.id, db=db))
        events = db.scalars(select(StatusEvent).where(StatusEvent.order_id == order.id)).all()
    assert any(e.note == "видалено з черги" and e.operator_id == user.id for e in events)


def test_email_order_has_no_sheet_row_to_clear():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db, source="email", sheet_tab=None, row_number=None, work_order_no=None)
        with patch.object(orders_router_mod, "clear_sheet_row_background") as clear:
            asyncio.run(orders_router_mod.delete_order(request=_request(user.id), order_id=order.id, db=db))
        clear.assert_not_called()
        assert db.get(Order, order.id).archived_at is not None


def test_deleting_twice_is_harmless():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        with patch.object(orders_router_mod, "clear_sheet_row_background") as clear:
            asyncio.run(orders_router_mod.delete_order(request=_request(user.id), order_id=order.id, db=db))
            first = db.get(Order, order.id).archived_at
            asyncio.run(orders_router_mod.delete_order(request=_request(user.id), order_id=order.id, db=db))
            # другий виклик не чіпає таблицю й не перештамповує дату
            assert clear.call_count == 1
            assert db.get(Order, order.id).archived_at == first


def test_delete_requires_login():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        order = _order(db)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(orders_router_mod.delete_order(request=_request(None), order_id=order.id, db=db))
    assert exc.value.status_code == 401


def test_delete_unknown_order_is_404():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(orders_router_mod.delete_order(request=_request(user.id), order_id=999, db=db))
    assert exc.value.status_code == 404


def test_htmx_delete_redirects_so_the_row_leaves_the_queue():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        with patch.object(orders_router_mod, "clear_sheet_row_background"):
            response = asyncio.run(
                orders_router_mod.delete_order(
                    request=_request(user.id, {"HX-Request": "true"}),
                    order_id=order.id, db=db,
                )
            )
    assert response.headers["HX-Redirect"] == "/"


def test_inline_delete_keeps_the_operator_in_place_and_still_toasts():
    """Deleting from a queue ROW must not redirect — the operator keeps the day
    tab, filters and scroll they were working in (the row itself is removed
    client-side). The toast still has to go out: it is the only feedback that
    the click did anything, and a silent delete button is not shippable."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        with patch.object(orders_router_mod, "clear_sheet_row_background"):
            response = asyncio.run(
                orders_router_mod.delete_order(
                    request=_request(user.id, {"HX-Request": "true"}),
                    order_id=order.id, inline="1", db=db,
                )
            )
        assert "HX-Redirect" not in response.headers
        assert "toast" in response.headers["HX-Trigger"]
        assert db.get(Order, order.id).archived_at is not None


def test_card_delete_still_redirects_to_the_queue():
    """Without `inline` the caller is the work card, whose page is gone once the
    work is archived — that one still hands the operator back to the queue."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        with patch.object(orders_router_mod, "clear_sheet_row_background"):
            response = asyncio.run(
                orders_router_mod.delete_order(
                    request=_request(user.id, {"HX-Request": "true"}),
                    order_id=order.id, inline="", db=db,
                )
            )
    assert response.headers["HX-Redirect"] == "/"


def test_dismissing_a_change_clears_it_and_records_who_looked():
    """The mark is cleared by the operator, never by a timer (user decision
    25.08.26): a change that expires on its own can expire during a break —
    exactly when it would have been missed. Who acknowledged it is kept."""
    from datetime import datetime

    from app.models import StatusEvent as SE

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = _order(db)
        order.sheet_changed_at = datetime(2026, 8, 25, 13, 42)
        order.sheet_changed_fields = "колір, шлях"
        db.commit()

        asyncio.run(
            orders_router_mod.dismiss_sheet_change(
                request=_request(user.id, {"HX-Request": "true"}),
                order_id=order.id, db=db,
            )
        )

        refreshed = db.get(Order, order.id)
        assert refreshed.sheet_changed_at is None
        assert refreshed.sheet_changed_fields is None
        notes = [e.note for e in db.scalars(select(SE).where(SE.order_id == order.id))]
        assert any("переглянув зміни техніка" in (n or "") for n in notes)


def test_dismiss_requires_login():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        order = _order(db)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                orders_router_mod.dismiss_sheet_change(request=_request(None), order_id=order.id, db=db)
            )
        assert exc.value.status_code == 401
