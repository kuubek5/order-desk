"""Табло пічок для логістів (11.09.26): окремий вхід у мережі, лише перегляд.

Стережемо:
1. На табло лише температура й час відкриття; збій — «немає даних», не число.
2. Окремий застосунок не віддає НІЧОГО, крім табло за правильним секретом і
   кількох файлів оформлення (решта — 404, зокрема сторінки KuubMill).
3. Вимкнене табло не слухає порт; вимикач реально відкриває й закриває його.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app.services import furnace_board as fb
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

KYIV = timezone(timedelta(hours=3))
_REAL_VIEW = fb.board_view
NOW = datetime(2026, 9, 10, 17, 42, tzinfo=KYIV)


def _card(name, *, running=False, idle=False, problem=False, temp=None, done_at=None, remaining=""):
    state = SimpleNamespace(temp_c=temp, done_at=done_at, remaining_text=remaining,
                            captured_at=datetime(2026, 9, 10, 17, 41))
    return SimpleNamespace(target=SimpleNamespace(name=name), state=state, is_running=running,
                           is_idle=idle, has_problem=problem, has_data=not problem)


CARDS = [
    _card("Піч 1", running=True, temp=1380, done_at=datetime(2026, 9, 11, 3, 15, tzinfo=KYIV), remaining="9 год 33 хв"),
    _card("Піч 2", running=True, temp=860, done_at=datetime(2026, 9, 10, 23, 40, tzinfo=KYIV), remaining="5 год 58 хв"),
    _card("Піч 3", idle=True, temp=42),
    _card("Допоміжна", problem=True, temp=999),
]


def test_board_shows_temperature_and_opening_only():
    view = fb.board_view(None, NOW, cards=CARDS)
    kinds = [(c.name, c.kind, c.temp, c.open_day, c.open_at) for c in view.cards]
    assert kinds == [
        ("Піч 1", "run", 1380, "завтра", "03:15"),
        ("Піч 2", "run", 860, "", "23:40"),
        ("Піч 3", "idle", 42, "", ""),
        ("Допоміжна", "bad", None, "", ""),
    ], "збій печі — без числа, навіть якщо в стані лежить старе"
    assert (view.nearest_name, view.nearest_at, view.nearest_day) == ("Піч 2", "23:40", "")
    assert view.cards[2].note == "", "«Можна завантажувати» прибрано (власник)"


def _enable(factory) -> str:
    from app.settings_store import set_setting

    with factory() as db:
        set_setting(db, fb.BOARD_ENABLED_KEY, "1")
        token = fb.regenerate_token(db)
        db.commit()
    return token


def test_board_app_serves_only_the_board(app_db, monkeypatch):  # noqa: F811
    from app.routers.furnace_board import create_board_app

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: _REAL_VIEW(db, NOW, cards=CARDS))
    board = MiniClient(create_board_app())

    status, headers, html = board.get(f"/t/{token}")
    assert status == 200 and "Піч 2" in html and "23:40" in html and "°C" in html
    assert headers.get("cache-control") == "no-store"
    status, _, frag = board.get(f"/t/{token}/cards")
    assert status == 200 and 'id="fb-live"' in frag

    for path in ("/t/wrong", "/t/wrong/cards", "/", "/settings", "/login", "/queue",
                 "/static/css/app.css", "/static/img/logo-kmill.svg", "/static/js/app.js"):
        assert board.get(path)[0] == 404, path
    assert board.get("/static/img/furnace-crowns.jpg")[0] == 200
    assert board.get("/static/css/fonts.css")[0] == 200


def test_disabled_board_answers_404_even_with_the_right_link(app_db, monkeypatch):  # noqa: F811
    from app.routers.furnace_board import create_board_app
    from app.settings_store import set_setting

    _, factory = app_db
    token = _enable(factory)
    with factory() as db:
        set_setting(db, fb.BOARD_ENABLED_KEY, "")
        db.commit()
    monkeypatch.setattr("app.db.SessionLocal", factory)
    assert MiniClient(create_board_app()).get(f"/t/{token}")[0] == 404


def test_settings_toggle_creates_a_link_and_regenerate_kills_the_old(app_db):  # noqa: F811
    _, factory = app_db
    app, _ = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    assert client.post("/settings/feedback/board/toggle", {})[0] == 303
    with factory() as db:
        assert fb.board_enabled(db)
        first = fb.board_token(db)
    assert first
    client.post("/settings/feedback/board/regenerate", {})
    with factory() as db:
        assert fb.board_token(db) not in (None, first)
    status, _, html = client.get("/settings/feedback")
    assert status == 200 and 'id="furnace-board"' in html and "Вимкнути табло" in html


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_switch_really_opens_and_closes_the_port(app_db, monkeypatch):  # noqa: F811
    """Перехід, а не стан: увімкнули — порт відповідає, вимкнули — закрився."""
    from app.routers.furnace_board import create_board_app
    from app.settings_store import set_setting

    _, factory = app_db
    token = _enable(factory)
    port = _free_port()
    monkeypatch.setattr(fb, "BOARD_PORT", port)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: _REAL_VIEW(db, NOW, cards=CARDS))
    stop = threading.Event()
    worker = threading.Thread(target=fb.board_worker, args=(stop, create_board_app), daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{port}/t/{token}"
    try:
        body = _wait_for(lambda: urllib.request.urlopen(url, timeout=2).read().decode())
        assert "Піч 1" in body
        assert fb.status_snapshot().listening

        with factory() as db:
            set_setting(db, fb.BOARD_ENABLED_KEY, "")
            db.commit()
        _wait_for(lambda: _refused(url))
        _wait_for(lambda: _not_listening())
    finally:
        stop.set()
        worker.join(timeout=15)


def _not_listening() -> bool:
    assert not fb.status_snapshot().listening
    return True


def _refused(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1)
    except (urllib.error.URLError, ConnectionError, OSError):
        return True
    raise AssertionError("порт досі відповідає")


def _wait_for(fn, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    pytest.fail(f"не дочекались: {last!r}")
