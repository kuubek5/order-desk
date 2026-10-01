"""Чіп верстата → RustDesk наперед (власник 30.09.26).

Що ламається тихо:
- на ПК, де стоїть CRM, RustDesk мусить запускати СЕРВЕР (інакше вікно лишається
  за браузером), а з іншого ПК — ні: там сервер віддає лише посилання;
- підміна `open_rustdesk` мусить влучати в модуль роута (CLAUDE.md §14), інакше
  тест запустив би справжній RustDesk;
- вікно обирається лише певне: нове або з ID/назвою в заголовку. Чуже не
  піднімаємо.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.platform_windows import pick_rustdesk_window
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура


def _machine(factory, rustdesk_id="123456789"):
    from app.models import Machine

    with factory() as db:
        m = Machine(name="350i L", host="10.0.0.7", port=5900, rustdesk_id=rustdesk_id,
                    created_at=datetime(2026, 9, 30))
        db.add(m)
        db.commit()
        return m.id


def _calls(monkeypatch):
    from app.routers import machines as machines_router

    calls = []
    monkeypatch.setattr(machines_router, "open_rustdesk", lambda *a: calls.append(a))
    return calls


def test_same_pc_opens_rustdesk_on_the_server(app_db, monkeypatch):  # noqa: F811
    app, factory = app_db
    mid = _machine(factory)
    calls = _calls(monkeypatch)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, body = client.post(f"/machines/{mid}/rustdesk")
    assert status == 200 and json.loads(body) == {"opened": True}
    assert calls == [("rustdesk://connection/new/123456789", "123456789", "350i L")]


def test_other_pc_gets_the_link_and_nothing_opens_on_the_server(app_db, monkeypatch):  # noqa: F811
    from app.settings_store import set_setting

    app, factory = app_db
    with factory() as db:
        set_setting(db, "network_access_enabled", "1")
    mid = _machine(factory)
    calls = _calls(monkeypatch)
    client = MiniClient(app, client_host="192.168.1.20")
    client.login(*ADMIN)
    status, _, body = client.post(f"/machines/{mid}/rustdesk")
    assert status == 200
    assert json.loads(body) == {"opened": False, "url": "rustdesk://connection/new/123456789"}
    assert calls == []


def test_machine_without_rustdesk_id_is_404(app_db, monkeypatch):  # noqa: F811
    app, factory = app_db
    mid = _machine(factory, rustdesk_id="")
    calls = _calls(monkeypatch)
    client = MiniClient(app)
    client.login(*ADMIN)
    assert client.post(f"/machines/{mid}/rustdesk")[0] == 404
    assert client.post("/machines/999/rustdesk")[0] == 404
    assert calls == []


def test_anonymous_is_refused(app_db, monkeypatch):  # noqa: F811
    app, factory = app_db
    mid = _machine(factory)
    calls = _calls(monkeypatch)
    assert MiniClient(app).post(f"/machines/{mid}/rustdesk")[0] == 401
    assert calls == []


def test_pick_window_prefers_new_then_title_and_never_guesses():
    windows = [(1, "RustDesk"), (2, "123 456 789 - 350i L"), (3, "987654321")]
    # Нове вікно сесії — його.
    assert pick_rustdesk_window(windows + [(9, "щось")], {1, 2, 3}, "123456789", "350i L") == (9, "нове вікно")
    # Вкладка в наявному вікні — за ID у заголовку (пробіли не заважають).
    assert pick_rustdesk_window(windows, {1, 2, 3}, "123456789", "") == (2, "за заголовком")
    # За назвою верстата.
    assert pick_rustdesk_window([(1, "RustDesk"), (5, "350i L")], {1, 5}, "555", "350i l") == (5, "за заголовком")
    # Нічого певного — нічого не піднімаємо (не чужу сесію й не головне вікно).
    assert pick_rustdesk_window(windows, {1, 2, 3}, "111", "250i") == (None, "")


def test_pick_window_by_real_shop_titles():
    """Заголовки з логу цеху 01.10.26: RustDesk пише `користувач@ПК@серійник -
    Remote Desktop - RustDesk`, а не ID і не нашу назву — вікно лишалось позаду."""
    olejka = (7, "150@Olejka@sn2023s1297 - Remote Desktop - RustDesk")
    loader = (8, "350@Loaderr@350i - Remote Desktop - RustDesk")
    main = (1, "RustDesk")
    # Слово з назви верстата в заголовку.
    assert pick_rustdesk_window([main, olejka, loader], {1, 7, 8}, "460103197", "150i-Olejka") == (7, "за словом назви")
    assert pick_rustdesk_window([main, olejka, loader], {1, 7, 8}, "226462038", "350i Loader") == (8, "за словом назви")
    # Єдине вікно сесії — нова вкладка відкрилась у ньому.
    assert pick_rustdesk_window([main, loader], {1, 8}, "294387522", "250i-Sec") == (8, "єдине вікно сесії")
    # Кілька сесій і слово не збіглось («Sec» закоротке) — не вгадуємо.
    assert pick_rustdesk_window([main, olejka, loader], {1, 7, 8}, "294387522", "250i-Sec") == (None, "")
    # Головне вікно RustDesk саме по собі сесією не є.
    assert pick_rustdesk_window([main], {1}, "294387522", "250i-Sec") == (None, "")
