"""Сторінка рядків черги (аудит 06.09.26, F1).

«Раніше» тримає до 30 днів робіт — 600+ рядків по ~9 КБ давали 5,5 МБ HTML,
і полл тягнув їх кожні 15 с. Тепер у HTML їде перша сторінка, решта — кнопкою
«Показати ще», яка свапає той самий контейнер з більшим `limit`.

Що стережемо:
1. лічильники «N у вигляді» та одиниці — по ВСЬОМУ списку, не по зрізу;
2. зріз не міняє порядку (CLAUDE.md §2) — це префікс того самого списку;
3. розкрита глибина переживає полл (`limit` потрапляє в rows_qs);
4. полл на великому списку сповільнюється, на звичайному — ні.
"""

from urllib.parse import parse_qs

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import business_today
from app.db import Base
from app.models import Order, User
from app.services.queue_view import (
    QUEUE_POLL_SLOW_ROWS,
    QUEUE_ROWS_MAX,
    QUEUE_ROWS_PAGE,
    QUEUE_SLOW_POLL_SECONDS,
    build_queue_view,
    clamp_rows_limit,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed(db, n):
    user = User(username="op", password_hash="x", full_name="op", role="оператор")
    db.add(user)
    tab = business_today().strftime("%d.%m.%y")
    for i in range(n):
        db.add(
            Order(
                source="lab",
                sheet_tab=tab,
                row_number=i + 1,
                work_order_no=str(20000 + i),
                quantity="2",
                status="нове",
            )
        )
    db.commit()
    return user


def test_clamp_keeps_limit_between_page_and_max():
    assert clamp_rows_limit(None) == QUEUE_ROWS_PAGE
    assert clamp_rows_limit("") == QUEUE_ROWS_PAGE
    assert clamp_rows_limit("abc") == QUEUE_ROWS_PAGE
    assert clamp_rows_limit(10) == QUEUE_ROWS_PAGE
    assert clamp_rows_limit(QUEUE_ROWS_PAGE + 200) == QUEUE_ROWS_PAGE + 200
    assert clamp_rows_limit(10**9) == QUEUE_ROWS_MAX


def test_small_queue_is_not_paged(db):
    user = _seed(db, 5)
    ctx = build_queue_view(db, user, period="today").context
    assert len(ctx["orders"]) == 5
    assert ctx["rows_total"] == 5
    assert ctx["rows_more"] == 0
    assert "limit=" not in ctx["rows_qs"]


def test_big_queue_sends_one_page_but_counts_everything(db):
    n = QUEUE_ROWS_PAGE + 37
    user = _seed(db, n)
    ctx = build_queue_view(db, user, period="today").context
    assert len(ctx["orders"]) == QUEUE_ROWS_PAGE
    assert ctx["rows_total"] == n
    assert ctx["rows_more"] == 37
    # Одиниці — по всьому списку (по 2 на роботу), а не по зрізу.
    assert ctx["total_units"] == 2 * n


def test_page_is_a_prefix_of_the_full_order(db):
    n = QUEUE_ROWS_PAGE + 10
    user = _seed(db, n)
    page = build_queue_view(db, user, period="today").context["orders"]
    full = build_queue_view(db, user, period="today", limit=QUEUE_ROWS_MAX).context["orders"]
    assert [o.id for o in page] == [o.id for o in full][:QUEUE_ROWS_PAGE]
    assert len(full) == n


def test_expanded_limit_survives_the_poll(db):
    user = _seed(db, QUEUE_ROWS_PAGE + 10)
    ctx = build_queue_view(db, user, period="today", limit=QUEUE_ROWS_PAGE * 2).context
    assert len(ctx["orders"]) == QUEUE_ROWS_PAGE + 10
    assert parse_qs(ctx["rows_qs"])["limit"] == [str(QUEUE_ROWS_PAGE * 2)]


def test_poll_slows_down_only_on_a_big_list(db):
    user = _seed(db, QUEUE_POLL_SLOW_ROWS + 1)
    ctx = build_queue_view(db, user, period="today").context
    assert ctx["poll_seconds"] >= QUEUE_SLOW_POLL_SECONDS
    small = build_queue_view(db, user, period="tomorrow").context
    assert small["poll_seconds"] == small["sync_screen_seconds"]
