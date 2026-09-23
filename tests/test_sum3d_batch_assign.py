"""Груповий Sum3D — один ID усьому набору «мої зараз» (`/orders/sum3d-batch`).

Бойовий випадок власника (23.09.26): один Sum3D-проєкт накриває кілька клієнтів
з пошти. Оператор пришпилює їх шпилькою, рахує в Sum3D і вписує отриманий id
ОДИН раз — портал кладе його в кожен рядок і бере роботи в роботу, знімаючи
шпильки, рівно як одиночна дія.

Стережемо саме те, що додала пачка поверх уже покритого `apply_sum3d`:
1. один id лягає в УСІ пришпилені придатні роботи, шпильки з них знімаються;
2. робота, що вже має Sum3D, НЕ перезаписується (пачка призначає новим);
3. кожне призначення — окрема undo-дія (лог на кожну роботу).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ActionLog, Order, User
from app.services.focus import focused_ids, toggle
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _email_order(db, client_name, material, *, sum3d=None, status="прийнято"):
    order = Order(
        source="email", client_name=client_name, material_color=material,
        quantity=1, status=status, sum3d_id=sum3d,
    )
    db.add(order)
    db.commit()
    return order


def test_batch_assigns_one_id_to_all_pinned_and_skips_taken(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        op = db.scalar(select(User).where(User.username == OPERATOR[0]))
        fresh_a = _email_order(db, "Струбицький", "mono a3")
        fresh_b = _email_order(db, "Кривовид", "zr")
        taken = _email_order(db, "Вже взято", "pmma a2", sum3d="09-00-00", status="прораховано")
        for order in (fresh_a, fresh_b, taken):
            toggle(db, order, op)  # усі троє в наборі «мої зараз»
        db.commit()
        ids = (fresh_a.id, fresh_b.id, taken.id)

    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 303)
    status, _, _ = client.post("/orders/sum3d-batch", {"sum3d_id": "12-01-45"})
    assert status in (200, 303)

    with session_factory() as db:
        op = db.scalar(select(User).where(User.username == OPERATOR[0]))
        a, b, t = (db.get(Order, i) for i in ids)
        # (1) один id ліг у обидві свіжі роботи
        assert a.sum3d_id == "12-01-45"
        assert b.sum3d_id == "12-01-45"
        # (2) вже взяту не перезаписано
        assert t.sum3d_id == "09-00-00"
        # шпильки знято лише зі змінених; вже взята лишається в наборі
        assert focused_ids(db, op) == {ids[2]}
        # (3) окрема undo-дія на кожну призначену роботу
        for i in ids[:2]:
            assert db.scalar(
                select(ActionLog).where(
                    ActionLog.order_id == i, ActionLog.action_type == "sum3d"
                )
            ) is not None


def test_batch_without_pinned_works_assigns_nothing(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        _email_order(db, "Не пришпилений", "mono a3")  # є робота, але не в наборі

    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, _ = client.post("/orders/sum3d-batch", {"sum3d_id": "12-01-45"})
    assert status in (200, 303)

    with session_factory() as db:
        assert all(o.sum3d_id is None for o in db.scalars(select(Order)))


def test_batch_rejects_empty_id(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        op = db.scalar(select(User).where(User.username == OPERATOR[0]))
        order = _email_order(db, "Струбицький", "mono a3")
        toggle(db, order, op)
        db.commit()
        oid = order.id

    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, _ = client.post("/orders/sum3d-batch", {"sum3d_id": "   "})
    assert status in (200, 303)

    with session_factory() as db:
        assert db.get(Order, oid).sum3d_id is None  # порожній id нічого не пише
