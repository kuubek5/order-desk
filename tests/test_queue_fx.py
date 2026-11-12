"""Вигляд черги (власник 12.11.26): смужка прогресу верстата в рядку й
вимикачі адміністратора. Рух (queue.js) перевіряється в браузері; тут — що
сервер малює смужку лише коли треба, і що вимикачі справді вимикають."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.business_day import business_tab_today
from app.services import machines as ms
from app.services import queue_fx
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

QUEUE = "/?period=today&source=all&ready=all"


@pytest.fixture
def milling(app_db):  # noqa: F811
    """Черга з двома роботами; перша зараз на верстаті (62 %)."""
    from app.models import Order

    app, factory = app_db
    tab = business_tab_today().strftime("%d.%m.%y")
    with factory() as db:
        db.add_all([
            Order(source="sheet_client", sheet_tab=tab, row_number=7, quantity="3", status="прораховано",
                  material_color="mono a2", client_name="Клієнт А", sum3d_id="09-12-40"),
            Order(source="sheet_client", sheet_tab=tab, row_number=8, quantity="1", status="прораховано",
                  material_color="mono a3", client_name="Клієнт Б", sum3d_id="10-00-00"),
        ])
        db.commit()
    target = ms.MachineTarget(name="250i-Sec", host="10.0.0.58", port=8765, agent_token="t")
    with ms._states_lock:
        ms._states.clear()
        ms._states[target.key] = ms.MachineState(target=target, frame_at=datetime.now(), percent=62,
                                                 sum3d_id="09-12-40")
    yield app, factory, target
    with ms._states_lock:
        ms._states.clear()


def test_flags_are_on_by_default(app_db):  # noqa: F811
    _app, factory = app_db
    with factory() as db:
        assert queue_fx.flags(db) == {"live": True, "motion": True}


def test_milling_row_gets_progress_bar_and_only_that_row(milling):
    app, _factory, _target = milling
    client = MiniClient(app)
    client.login(*ADMIN)
    html = client.get(QUEUE)[2]
    assert html.count('class="q-live"') == 1, "смужка лише в роботи на верстаті"
    assert '--p:0.620' in html and 'data-p="62"' in html
    assert html.count("queue-row-live") == 1
    assert 'data-fx-motion="1"' in html
    # Той самий стан у відповіді полла — інакше смужка зникала б кожні 15 с.
    rows = client.get(QUEUE + "&partial=rows")[2]
    assert 'class="q-live"' in rows and 'data-fx-motion="1"' in rows


def test_no_bar_without_a_read_percent(milling):
    app, _factory, target = milling
    with ms._states_lock:
        ms._states[target.key].percent = None
    client = MiniClient(app)
    client.login(*ADMIN)
    html = client.get(QUEUE)[2]
    assert 'class="q-live' not in html, "порожнє поле краще за вигадане"
    assert "badge milling" in html, "чіп верстата лишається"


def test_admin_switches_turn_both_off_operator_cannot(milling):
    app, factory, _target = milling
    admin = MiniClient(app)
    admin.login(*ADMIN)
    settings = admin.get("/settings")[2]
    assert 'data-sec="queue-fx"' in settings

    assert admin.post("/settings/queue-fx", {})[0] == 303  # обидві галочки зняті
    with factory() as db:
        assert queue_fx.flags(db) == {"live": False, "motion": False}
    html = admin.get(QUEUE)[2]
    assert 'class="q-live' not in html and "queue-row-live" not in html
    assert "data-fx-motion" not in html
    assert "badge milling" in html, "чіп верстата не залежить від вимикача"

    assert admin.post("/settings/queue-fx", {"live": "1"})[0] == 303
    with factory() as db:
        assert queue_fx.flags(db) == {"live": True, "motion": False}

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert 'data-sec="queue-fx"' not in operator.get("/settings")[2]
    assert operator.post("/settings/queue-fx", {"live": "1", "motion": "1"})[0] == 403
    with factory() as db:
        assert queue_fx.flags(db) == {"live": True, "motion": False}
