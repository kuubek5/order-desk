"""Бейдж «Черга» в рейці (власник 29.09.26): скільки СЬОГОДНІШНІХ робіт
лабораторії можна брати — те саме число, що на чіпах «Сьогодні · Лабораторія
· Можна брати»."""

from __future__ import annotations

import pytest

from app.business_day import business_tab_today, prev_tab_day
from app.models import Order
from app.queue_filters import count_by_readiness
from app.routers.deps import clear_global_badge_cache, queue_can_take_count_uncached
from app.services.order_dates import order_date
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _seed(db):
    today = business_tab_today().strftime("%d.%m.%y")
    yesterday = prev_tab_day(business_tab_today()).strftime("%d.%m.%y")
    rows = [
        Order(sheet_tab=today, status="нове", source="lab", job_code="P:/a"),  # рахується
        Order(sheet_tab=today, status="нове", source="lab", job_code="P:/b", sum3d_id="12-01-45"),  # в роботі
        Order(sheet_tab=today, status="нове", source="lab"),  # не готово
        Order(sheet_tab=today, status="нове", source="email", client_name="Клініка"),  # клієнт
        Order(sheet_tab=yesterday, status="нове", source="lab", job_code="P:/c"),  # вчора
    ]
    db.add_all(rows)
    db.commit()


def test_counts_only_todays_lab_works_that_can_be_taken(app_db):  # noqa: F811
    _, session_factory = app_db
    with session_factory() as db:
        _seed(db)
        # Те саме число, що покаже чіп у черзі: ті самі предикати.
        lab_today = [
            o for o in db.query(Order).all()
            if o.source == "lab" and order_date(o) == business_tab_today()
        ]
        chip = count_by_readiness(lab_today)["can_take"]
    assert queue_can_take_count_uncached() == chip == 1


def test_rail_shows_the_badge(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        _seed(db)
    clear_global_badge_cache()
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, body = client.get("/mail")
    assert status == 200
    html = body.decode("utf-8") if isinstance(body, bytes) else body
    rail_queue = html.split('<span class="rail-label">Черга</span>', 1)[1].split("</a>", 1)[0]
    assert 'class="mail-nav-count tnum"' in rail_queue and ">1</span>" in rail_queue


def test_notify_state_carries_the_same_ids_for_the_live_badge(app_db):  # noqa: F811
    """Живий бейдж (05.10.26): /api/notify-state віддає ті самі id, що рахує
    бейдж у рейці, — браузер оновлює число й «+N нових» з цього ж опитування."""
    app, session_factory = app_db
    with session_factory() as db:
        _seed(db)
        expected = [o.id for o in db.query(Order).all()
                    if o.source == "lab" and o.job_code == "P:/a"]
    clear_global_badge_cache()
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, body = client.get("/api/notify-state")
    assert status == 200
    import json
    state = json.loads(body)
    assert state["can_take_ids"] == expected
    assert len(state["can_take_ids"]) == queue_can_take_count_uncached()


def test_empty_badges_stay_in_the_rail_hidden(app_db):  # noqa: F811
    """Порожній бейдж лишається в розмітці з `hidden`: інакше живе оновлення
    не мало б що показати, коли робота зʼявиться."""
    app, _ = app_db
    clear_global_badge_cache()
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, body = client.get("/mail")
    html = body.decode("utf-8") if isinstance(body, bytes) else body
    rail_queue = html.split('<span class="rail-label">Черга</span>', 1)[1].split("</a>", 1)[0]
    assert 'data-live="queue"' in rail_queue and " hidden>0</span>" in rail_queue
    assert 'data-live="queue-new"' in rail_queue
    assert 'data-live="mail"' in html
