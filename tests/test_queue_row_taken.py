"""Щойно взята робота підсвічується дві хвилини (власник 05.11.26).

Щоб очима швидко знайти останню прораховану. Набір спільний для всіх операторів і
рахує лише дії людини: синк не має підсвічувати цілу вкладку.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import utc_now
from app.models import Base, Order, StatusEvent, User
from app.services.focus import TAKEN_HIGHLIGHT_SECONDS, recently_taken_ids
from app.statuses import STATUS_ACCEPTED, STATUS_CALCULATED, STATUS_NEW
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _session() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _order(db, **fields) -> Order:
    order = Order(source="lab", status=STATUS_CALCULATED, **fields)
    db.add(order)
    db.flush()
    return order


def _user(db) -> User:
    user = User(username="op", password_hash="x", full_name="op", role="оператор")
    db.add(user)
    db.flush()
    return user


def _event(db, order, user, *, ago: float = 5, status=STATUS_CALCULATED, note=None, by_human=True):
    db.add(StatusEvent(
        order_id=order.id, operator_id=user.id if by_human else None, status=status, note=note,
        actor=user.username if by_human else "sync",
        occurred_at=utc_now() - timedelta(seconds=ago),
    ))
    db.flush()


def test_fresh_action_of_a_person_is_highlighted():
    with _session() as db:
        user = _user(db)
        calculated, accepted = _order(db), _order(db)
        _event(db, calculated, user)
        _event(db, accepted, user, status=STATUS_ACCEPTED)
        assert recently_taken_ids(db) == {calculated.id, accepted.id}


def test_highlight_ends_after_two_minutes():
    with _session() as db:
        user = _user(db)
        order = _order(db)
        _event(db, order, user, ago=TAKEN_HIGHLIGHT_SECONDS - 5)
        assert recently_taken_ids(db) == {order.id}
        later = utc_now() + timedelta(seconds=10)
        assert recently_taken_ids(db, now=later) == set()


@pytest.mark.parametrize("kwargs, why", [
    ({"by_human": False}, "синк побачив готовий рядок: міг би підсвітити цілу вкладку"),
    ({"note": "скасовано (Sum3D)"}, "скасування, а не взяття в роботу"),
    ({"status": STATUS_NEW}, "повернення в «нове» не є взяттям"),
])
def test_other_events_do_not_highlight(kwargs, why):
    with _session() as db:
        user = _user(db)
        _event(db, _order(db), user, **kwargs)
        assert recently_taken_ids(db) == set(), why


def test_all_works_of_one_disc_are_highlighted_together():
    """Мультипрорахунок: один Sum3D ID на кілька робіт, шукати треба всі."""
    with _session() as db:
        user = _user(db)
        orders = [_order(db, sum3d_id="13-12-29") for _ in range(3)]
        for order in orders:
            _event(db, order, user)
        assert recently_taken_ids(db) == {o.id for o in orders}


def test_event_older_than_the_scan_window_is_ignored():
    """Запит читає лише останні події, тож навіть із довгою історією він швидкий."""
    with _session() as db:
        user = _user(db)
        old = _order(db)
        _event(db, old, user, ago=TAKEN_HIGHLIGHT_SECONDS * 10)
        fresh = _order(db)
        _event(db, fresh, user)
        assert recently_taken_ids(db) == {fresh.id}


def _seed_row(factory, *, seconds_ago: float) -> int:
    from app.business_day import business_tab_today

    with factory() as db:
        user = db.query(User).filter_by(username=OPERATOR[0]).one()
        order = Order(
            source="lab", status=STATUS_CALCULATED, work_order_no="99001", job_code="J1",
            sum3d_id="13-12-29", row_number=7, sheet_tab=business_tab_today().strftime("%d.%m.%y"),
        )
        db.add(order)
        db.flush()
        db.add(StatusEvent(
            order_id=order.id, operator_id=user.id, status=STATUS_CALCULATED, actor=user.username,
            occurred_at=utc_now() - timedelta(seconds=seconds_ago),
        ))
        user.mail_dock_scope = "labonly"
        user.mail_dock_ready = "all"
        db.commit()
        return order.id


def _row_tag(html: str, order_id: int) -> str:
    end = html.index(f'id="order-row-{order_id}"')
    return html[html.rindex("<tr", 0, end):end]


@pytest.mark.parametrize("path", ["/?period=today&ready=all&source=all", "/mail/queue-mirror"])
def test_queue_and_dock_both_mark_the_row(app_db, path):  # noqa: F811
    app, factory = app_db
    order_id = _seed_row(factory, seconds_ago=10)
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, html = client.get(path)
    assert "queue-row-taken" in _row_tag(html, order_id)


def test_expired_row_has_no_mark(app_db):  # noqa: F811
    app, factory = app_db
    order_id = _seed_row(factory, seconds_ago=TAKEN_HIGHLIGHT_SECONDS + 30)
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, html = client.get("/mail/queue-mirror")
    assert "queue-row-taken" not in _row_tag(html, order_id)


def test_row_rendered_after_an_action_gets_the_mark_too():
    """Рядок, що повертається після дії, йде через `_row_context`: без ключа він
    втратив би підсвітку саме в мить, коли оператор вписав Sum3D ID."""
    source = Path("app/routers/orders.py").read_text(encoding="utf-8")
    start = source.index("def _row_context")
    assert '"taken_ids"' in source[start:source.index("@router", start)]


def test_colour_is_a_token_and_layer_does_not_touch_cells():
    tokens = Path("app/static/css/tokens.css").read_text(encoding="utf-8")
    assert "--row-taken-rgb:" in tokens
    css = Path("app/static/css/v2a_queue.css").read_text(encoding="utf-8")
    assert "tr.queue-row-taken > td" not in css, "фон комірок належить стану рядка"
