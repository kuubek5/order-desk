"""«Записати рядок наново» — поштова робота, чий рядок зник ВИПАДКОВО (22.10.26).

Лише для стану «рядка немає» (`sheet_row_lost_at`) і лише пошта. Робота стає
рівно тим, що перевіряє `pending_row_clause()` (row_number None +
`sheet_row_pending`), і далі працює наявний повтор рядків пошти: спершу
«нічий» рядок із тим самим ключем і Sum3D, лише тоді дописування. Імена
вигадані.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionLog, Order, User
from app.services import sheet_row_actions
from app.services import sheet_stuck_writes as stuck
from app.services.mail_row_retry import append_pending_mail_row_warm, pending_row_clause
from tests.asgi_client import MiniClient
from tests.test_multicalc_safety import _client_line, _raw, _wire_sheet, rowdb  # noqa: F401
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

LOST_AT = datetime(2026, 10, 21, 5, 54)


@pytest.fixture(autouse=True)
def _clean():
    stuck.reset()
    yield
    stuck.reset()


def _lost_mail(db, **kw) -> Order:
    base = dict(
        source="email", sheet_tab="20.10.26", row_number=74, client_name="Клієнт-А",
        material_color="mono a3", quantity="1", sum3d_id="10-19-48", status="прораховано",
        sheet_row_lost_at=LOST_AT,
    )
    base.update(kw)
    order = Order(**base)
    db.add(order)
    db.commit()
    return order


def _user(db) -> User:
    user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(user)
    db.commit()
    return user


def test_start_puts_exactly_the_pending_row_state(rowdb):  # noqa: F811
    with Session(rowdb, expire_on_commit=False) as db:
        order = _lost_mail(db)
        sheet_row_actions.start_rewrite_row(db, order, _user(db))
        assert order.row_number is None and order.sheet_row_pending is not None
        assert order.sheet_row_lost_at is None
        assert db.scalar(select(Order.id).where(*pending_row_clause())) == order.id
        entry = db.scalar(select(ActionLog).where(ActionLog.action_type == "rewrite_row"))
        assert entry.old_value == "74"
        assert order.fill_pending is None, "не видана — заливку не чіпаємо"


def test_rewrite_binds_the_row_put_back_by_hand(rowdb, monkeypatch):  # noqa: F811
    """Рядок повернули руками (або він зʼїхав) — CRM його знаходить, а не дописує другий."""
    appended: list = []
    line = _client_line("Клієнт-А", "mono a3", "1", "10-19-48")
    _wire_sheet(monkeypatch, _raw(_client_line("Інший", "pmma a2", "2"), line), appended)
    with Session(rowdb, expire_on_commit=False) as db:
        db.add(Order(source="email", sheet_tab="20.10.26", row_number=1, client_name="Інший",
                     material_color="pmma a2", quantity="2", status="нове"))
        order = _lost_mail(db)
        sheet_row_actions.start_rewrite_row(db, order, _user(db))
        order_id = order.id

    assert append_pending_mail_row_warm(order_id) is None
    assert appended == []
    with Session(rowdb) as db:
        order = db.get(Order, order_id)
        assert order.row_number == 2 and order.sheet_row_pending is None


def test_rewrite_appends_when_the_row_is_really_gone_and_clears_blue_for_issued(rowdb, monkeypatch):  # noqa: F811
    appended: list = []
    _wire_sheet(monkeypatch, _raw(_client_line("Лаб-Б", "mono a3", "1", "16-20-34")), appended)
    with Session(rowdb, expire_on_commit=False) as db:
        db.add(Order(source="email", sheet_tab="20.10.26", row_number=1, client_name="Лаб-Б",
                     material_color="mono a3", quantity="1", sum3d_id="16-20-34", status="нове"))
        order = _lost_mail(db, status="знайдено при видачі")
        sheet_row_actions.start_rewrite_row(db, order, _user(db))
        assert order.fill_pending == "clear", "видана — синій нового рядка має зійти"
        order_id = order.id

    assert append_pending_mail_row_warm(order_id) is None
    assert appended == [("Клієнт-А", "1", "mono a3", "10-19-48")]
    with Session(rowdb) as db:
        order = db.get(Order, order_id)
        assert order.row_number == 2 and order.sheet_row_pending is None


def test_only_lost_live_mail_works_can_be_rewritten():
    assert sheet_row_actions.can_rewrite_row(Order(source="email", sheet_row_lost_at=LOST_AT))
    assert not sheet_row_actions.can_rewrite_row(Order(source="sheet_client", sheet_row_lost_at=LOST_AT))
    assert not sheet_row_actions.can_rewrite_row(Order(source="email"))
    assert not sheet_row_actions.can_rewrite_row(
        Order(source="email", sheet_row_lost_at=LOST_AT, archived_at=datetime.now())
    )


def _client(app):
    client = MiniClient(app)
    client.login(*OPERATOR)
    return client


def test_banner_offers_rewrite_only_for_lost_mail(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        mail = _lost_mail(db)
        typed = _lost_mail(db, source="sheet_client", client_name="Клієнт-Б", row_number=75)
        stuck.observe(db, now=1000.0)

    _, _, html = _client(app).get("/sheets/write-failures")
    assert f'hx-post="/sheets/write-failures/{mail.id}/rewrite-row"' in html
    assert f"/sheets/write-failures/{typed.id}/rewrite-row" not in html
    assert html.count("Рядок видалено свідомо — прибрати з CRM") == 2

    _, _, page = _client(app).get(f"/orders/{mail.id}")
    assert f"/sheets/write-failures/{mail.id}/rewrite-row" in page
    _, _, page = _client(app).get(f"/orders/{typed.id}")
    assert "/rewrite-row" not in page


def test_route_starts_the_existing_retry_and_reports(app_db, monkeypatch):  # noqa: F811
    from concurrent.futures import Future

    from app.services import mail_row_retry

    app, session_factory = app_db
    with session_factory() as db:
        order = _lost_mail(db)
        stuck.observe(db, now=1000.0)
    called: list = []

    def fake_now(db, ids, **_):
        called.append(set(ids))
        with session_factory() as s:  # «повтор» знайшов рядок 90
            o = s.get(Order, order.id)
            o.row_number, o.sheet_row_pending = 90, None
            s.commit()
        done: Future = Future()
        done.set_result(None)
        return [done]

    monkeypatch.setattr(mail_row_retry, "retry_pending_mail_rows_now", fake_now)
    monkeypatch.setattr("app.sheets.quota_is_tight", lambda: False)
    status, headers, body = _client(app).post(f"/sheets/write-failures/{order.id}/rewrite-row")

    assert status == 200 and called == [{order.id}]
    toast = json.loads(headers["hx-trigger"])["toast"]
    assert toast["kind"] == "success" and "рядок 96" in toast["message"]
    assert "swf-banner" not in body


def test_route_refuses_a_work_that_is_not_lost(app_db, monkeypatch):  # noqa: F811
    from app.services import mail_row_retry

    app, session_factory = app_db
    with session_factory() as db:
        order = _lost_mail(db, sheet_row_lost_at=None)
    monkeypatch.setattr(mail_row_retry, "retry_pending_mail_rows_now",
                        lambda *a, **k: pytest.fail("повтор не мав стартувати"))
    monkeypatch.setattr("app.sheets.quota_is_tight", lambda: False)
    _, headers, _ = _client(app).post(f"/sheets/write-failures/{order.id}/rewrite-row")
    assert "лише поштовій роботі" in json.loads(headers["hx-trigger"])["toast"]["message"]
    with session_factory() as db:
        assert db.get(Order, order.id).row_number == 74
