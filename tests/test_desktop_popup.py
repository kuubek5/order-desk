"""Власне вікно-сповіщення KuubMill (власник 05.10.26, `app/services/desktop_popup.py`).

Без GUI: налаштування, правило «коли показувати», виявлення подій, присутність
браузера й гейти роутів. Саме вікно (tkinter) тут не створюється.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.services import desktop_popup as dp
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_defaults_are_off_and_sane(db):
    s = dp.load_settings(db)
    assert s.enabled is False
    assert s.events == frozenset({"lab", "mail"})
    assert (s.anchor, s.monitor, s.xy, s.seconds) == ("br", 0, None, 8)


def test_save_roundtrip_and_clamps(db):
    dp.save_settings(db, enabled=True, events={"mail"}, anchor="tl", monitor=1, seconds=999)
    s = dp.load_settings(db)
    assert (s.enabled, s.events, s.anchor, s.monitor, s.seconds) == (True, frozenset({"mail"}), "tl", 1, 60)
    dp.save_settings(db, enabled=True, events=set(), anchor="bogus", monitor=-3, seconds=1)
    s = dp.load_settings(db)
    assert (s.events, s.anchor, s.monitor, s.seconds) == (frozenset(), "br", 0, 3)
    dp.save_settings(db, enabled=True, events={"lab"}, anchor="br", monitor=0, seconds=0)
    assert dp.load_settings(db).seconds == 0  # до кліку


def test_custom_anchor_needs_saved_coordinates(db):
    dp.save_settings(db, enabled=True, events={"lab"}, anchor="custom", monitor=0, seconds=8)
    assert dp.load_settings(db).anchor == "br"  # своє місце без координат — кут
    dp.save_custom_xy(db, 1200, -40)
    s = dp.load_settings(db)
    assert (s.anchor, s.xy) == ("custom", (1200, -40))


P = dp.Presence


@pytest.mark.parametrize("presence, kind, expected", [
    # Браузер давно мовчить (закрито / вкладка спить) — показуємо.
    (P(page="mail", visible=True, focused=True, at=0.0), "mail", True),
    # Згорнуто або інша вкладка — показуємо все.
    (P(page="mail", visible=False, focused=False, at=100.0), "mail", True),
    (P(page="queue", visible=False, focused=False, at=100.0), "lab", True),
    # У фокусі — нічого, досить тосту.
    (P(page="other", visible=True, focused=True, at=100.0), "lab", False),
    (P(page="other", visible=True, focused=True, at=100.0), "mail", False),
    # Видно без фокусу (другий монітор): пошта ховає листи, черга — лабораторію.
    (P(page="mail", visible=True, focused=False, at=100.0), "mail", False),
    (P(page="mail", visible=True, focused=False, at=100.0), "lab", True),
    (P(page="queue", visible=True, focused=False, at=100.0), "lab", False),
    (P(page="queue", visible=True, focused=False, at=100.0), "mail", True),
    (P(page="other", visible=True, focused=False, at=100.0), "lab", True),
])
def test_should_show_follows_the_owner_rules(presence, kind, expected):
    assert dp.should_show(kind, presence, now=110.0) is expected


def test_watcher_first_tick_is_baseline_then_reports_arrivals():
    w = dp.PopupWatcher()
    assert w.tick([(1, "24120 · моно A3 · 4 од.")], 5) == []
    # Взяли першу, прийшли дві нові — подія про нові, число = поточне.
    events = w.tick([(2, "24121 · пмма A2 · 2 од."), (3, "24122 · моно A3 · 4 од.")], 5)
    assert [(e.kind, e.count, e.title) for e in events] == [("lab", 2, "Лабораторія — можна брати 2")]
    assert events[0].body == "Нова: 24122 · моно A3 · 4 од. і ще 1"
    assert w.tick([(2, ""), (3, "")], 5) == []  # нічого нового
    mail = w.tick([(2, ""), (3, "")], 7)
    assert [(e.kind, e.count, e.title, e.path) for e in mail] == [("mail", 7, "2 нові листи", "/mail")]
    assert w.tick([(2, ""), (3, "")], 4) == []  # листів поменшало — не подія


def test_presence_from_a_background_tab_does_not_override_the_visible_one():
    dp.note_presence("queue", visible=True, focused=False, origin="http://x", now=1000.0)
    dp.note_presence("mail", visible=False, focused=False, origin="http://x", now=1001.0)
    assert dp.current_presence().page == "queue"
    dp.note_presence("mail", visible=False, focused=False, origin="http://x", now=1010.0)
    assert dp.current_presence().page == "mail"  # видимий звіт уже старий


def test_popup_routes_refuse_requests_from_the_network(app_db):  # noqa: F811
    app, _ = app_db
    remote = MiniClient(app, client_host="192.168.1.50")
    remote.login(*OPERATOR)
    for path in ("/settings/desktop-popup", "/settings/desktop-popup/test", "/settings/desktop-popup/place"):
        status, _, _ = remote.post(path, {"enabled": "1"})
        assert status in (403, 401), (path, status)


def test_save_route_on_this_pc_stores_the_whole_form(app_db):  # noqa: F811
    app, session_factory = app_db
    local = MiniClient(app)
    local.login(*OPERATOR)
    status, _, _ = local.post("/settings/desktop-popup", {
        "enabled": "1", "events": "lab", "anchor": "tr", "monitor": "0", "seconds": "15",
    })
    assert status == 200
    with session_factory() as db:
        s = dp.load_settings(db)
    assert (s.enabled, s.events, s.anchor, s.seconds) == (True, frozenset({"lab"}), "tr", 15)
    local.post("/settings/desktop-popup", {"events": "lab", "anchor": "tr", "monitor": "0", "until_click": "1"})
    with session_factory() as db:
        s = dp.load_settings(db)
    assert (s.enabled, s.seconds) == (False, 0)  # знята галочка = вимкнено


def test_presence_is_taken_only_from_this_pc(app_db):  # noqa: F811
    app, _ = app_db
    dp.note_presence("other", visible=False, focused=False, origin="", now=0.0)
    remote = MiniClient(app, client_host="192.168.1.50")
    remote.login(*OPERATOR)
    remote.get("/api/notify-state?page=mail&vis=1&focus=1&origin=http://remote")
    assert dp.current_presence().origin != "http://remote"
    local = MiniClient(app)
    local.login(*OPERATOR)
    local.get("/api/notify-state?page=queue&vis=1&focus=0&origin=http://127.0.0.1:8000")
    p = dp.current_presence()
    assert (p.page, p.visible, p.focused, p.origin) == ("queue", True, False, "http://127.0.0.1:8000")


def test_account_page_shows_the_block_and_says_where_it_is_configured(app_db):  # noqa: F811
    app, _ = app_db
    local = MiniClient(app)
    local.login(*OPERATOR)
    _, _, body = local.get("/account")
    html = body.decode("utf-8") if isinstance(body, bytes) else body
    assert "Спливаюче вікно KuubMill" in html and 'data-dn-form' in html
    remote = MiniClient(app, client_host="192.168.1.50")
    remote.login(*OPERATOR)
    _, _, body = remote.get("/account")
    html = body.decode("utf-8") if isinstance(body, bytes) else body
    assert "inert" in html.split("data-dn-form", 1)[1][:40] or "Налаштовується на ПК" in html


def test_worker_pass_shows_new_lab_work_and_respects_presence(monkeypatch):
    """Справжній цикл `web._desktop_popup_worker`: база → подія → правило →
    вікно. Вікно підмінене записом; три проходи: база, нова робота при
    згорнутому браузері (показано), ще одна при відкритій черзі без фокусу
    (пропущено — її і так видно)."""
    import threading

    from app import web
    from app.business_day import business_tab_today
    from app.routers.deps import clear_global_badge_cache

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    def factory():
        return Session(engine, expire_on_commit=False)

    shown: list[tuple] = []

    class FakeUI:
        def show(self, *args):
            shown.append(args)

    # Присутність — стан процесу; попередні тести лишають свіжий звіт видимої
    # вкладки, який (правильно) не перебивається фоновою. Починаємо з чистого.
    monkeypatch.setattr(dp, "_presence", dp.Presence())
    monkeypatch.setattr(web, "SessionLocal", factory)
    monkeypatch.setattr("app.routers.deps.SessionLocal", factory)
    monkeypatch.setattr(dp, "supported", lambda: True)
    monkeypatch.setattr(dp, "get_popup_ui", lambda: FakeUI())
    monkeypatch.setattr(web, "DESKTOP_POPUP_INITIAL_DELAY_SECONDS", 0)
    tab = business_tab_today().strftime("%d.%m.%y")
    with factory() as db:
        dp.save_settings(db, enabled=True, events={"lab", "mail"}, anchor="br", monitor=0, seconds=8)
        db.commit()

    stop = threading.Event()
    steps = iter([
        lambda: None,  # перший прохід — база
        lambda: (dp.note_presence("queue", visible=False, focused=False, origin=""),
                 _add(factory, tab, "P:/a", "24122")),
        lambda: (dp.note_presence("queue", visible=True, focused=False, origin=""),
                 _add(factory, tab, "P:/b", "24123")),
    ])

    done = {"v": False}

    def fake_wait(_timeout=None):
        clear_global_badge_cache()
        step = next(steps, None)
        if step is None:
            done["v"] = True
            return True
        step()
        return False

    monkeypatch.setattr(stop, "wait", fake_wait)
    monkeypatch.setattr(stop, "is_set", lambda: done["v"])
    web._desktop_popup_worker(stop)
    assert [(a[0], a[1], a[2]) for a in shown] == [("lab", 1, "Лабораторія — можна брати 1")]
    assert shown[0][3] == "Нова: 24122"


def _add(factory, tab, job_code, wo):
    from app.models import Order

    with factory() as db:
        db.add(Order(source="lab", sheet_tab=tab, status="нове", job_code=job_code, work_order_no=wo))
        db.commit()
