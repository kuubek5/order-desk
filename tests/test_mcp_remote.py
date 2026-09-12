"""Доступ до `/mcp` з іншої машини: окремий слухач, межа — токен.

Чому це окремий файл, а не ще кілька тестів у `test_mcp.py`: там перевіряється
протокол і межа loopback головного застосунку, а тут — ДРУГИЙ застосунок, у
якому гейт інший і в якому головне не те, що він віддає, а те, чого він НЕ
віддає.

Стережемо:
1. Без токена й з чужим токеном — 401; правильний токен приймається і з
   `Authorization: Bearer`, і з `X-Token`.
2. На цьому порту немає нічого, крім `POST /mcp`: ні кореня, ні статики, ні
   екранів KuubMill (решта — 404). Порт дивиться в мережу, тож «майже нічого»
   тут не годиться.
3. Токен не світиться у відповіді — вона їде назовні й не має бути підказкою.
4. `log_config=None` у конфігу uvicorn. Прод зібраний без консолі
   (`sys.stdout is None`), і без цього рядка слухач не відкриває порт узагалі —
   так у 0.15.5 мовчало табло печей, а dev із консоллю був зелений.
"""

from __future__ import annotations

import json
import threading

import pytest

from app.services import mcp_gateway as mg
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

WRONG = "zovsim-ne-toy-token"


def _enable(factory) -> str:
    from app.settings_store import set_setting

    with factory() as db:
        set_setting(db, mg.ENABLED_KEY, "1")
        token = mg.regenerate_token(db)
        db.commit()
    return token


def _remote(monkeypatch, factory) -> MiniClient:
    from app.routers.mcp import create_mcp_app

    monkeypatch.setattr("app.db.SessionLocal", factory)
    return MiniClient(create_mcp_app())


def _list_tools(client: MiniClient, headers: dict):
    return client.post_json(
        "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=headers
    )


# ── Гейт ────────────────────────────────────────────────────────────────────


def test_without_a_token_the_listener_answers_401(app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    _enable(factory)
    status, _, body = _list_tools(_remote(monkeypatch, factory), {})
    assert status == 401, body
    assert _json(body)["error"]["code"] == -32600


def test_a_wrong_token_answers_401_and_says_nothing_about_the_right_one(app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    token = _enable(factory)
    client = _remote(monkeypatch, factory)
    for headers in ({"Authorization": f"Bearer {WRONG}"}, {"X-Token": WRONG}):
        status, _, body = _list_tools(client, headers)
        assert status == 401, headers
        assert token not in body and token[:8] not in body, "токен не має світитись у відповіді"


def test_bearer_token_opens_the_tool_list(app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    token = _enable(factory)
    status, _, body = _list_tools(
        _remote(monkeypatch, factory), {"Authorization": f"Bearer {token}"}
    )
    assert status == 200, body
    names = {tool["name"] for tool in _json(body)["result"]["tools"]}
    assert "kmill_queue" in names and len(names) > 1
    assert token not in body


def test_x_token_header_works_the_same(app_db, monkeypatch):  # noqa: F811
    """Два заголовки, бо клієнти різні: MCP-клієнт шле `Authorization`, а ручна
    перевірка з `curl` простіше робиться `X-Token`."""
    _, factory = app_db
    token = _enable(factory)
    status, _, body = _list_tools(_remote(monkeypatch, factory), {"X-Token": token})
    assert status == 200, body
    assert {t["name"] for t in _json(body)["result"]["tools"]}


def test_token_in_the_address_works_too(app_db, monkeypatch):  # noqa: F811
    """`?t=` — те, що робить рядок на екрані налаштувань ОДНИМ.

    Власник копіює `http://…:8011/mcp?t=…` і передає як є; складати адресу з
    токеном руками не треба (`mcp_gateway.connect_links`). Якщо ця гілка
    відвалиться, екран і далі показуватиме гарний рядок, який нікуди не
    підключається — тому вона тут."""
    _, factory = app_db
    token = _enable(factory)
    client = _remote(monkeypatch, factory)
    status, _, body = client.post_json(
        f"/mcp?t={token}", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert status == 200, body
    assert "kmill_queue" in {tool["name"] for tool in _json(body)["result"]["tools"]}

    status, _, body = client.post_json(
        f"/mcp?t={WRONG}", {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert status == 401, body


def test_a_live_token_is_refused_while_the_switch_is_off(app_db, monkeypatch):  # noqa: F811
    """Порт закриває сторож, але до пʼяти секунд після «вимкнув» він ще живий —
    і в ці секунди вимкнений доступ мусить бути вимкненим (як у табло печей)."""
    _, factory = app_db
    token = _enable(factory)
    from app.settings_store import set_setting

    with factory() as db:
        set_setting(db, mg.ENABLED_KEY, "")
        db.commit()
    status, _, _ = _list_tools(_remote(monkeypatch, factory), {"X-Token": token})
    assert status == 401


# ── Нічого, крім /mcp ───────────────────────────────────────────────────────


def test_the_listener_serves_nothing_but_post_mcp(app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    token = _enable(factory)
    client = _remote(monkeypatch, factory)
    for path in ("/", "/mcp", "/static/app.css", "/static/css/app.css", "/static/js/app.js",
                 "/queue", "/settings", "/login", "/handout", "/t/" + token):
        assert client.get(path)[0] == 404, path
    # Форма на /mcp — теж не вхід: тіло не JSON-RPC, а гейт спрацював раніше.
    assert client.post("/queue", {})[0] == 404
    assert client.post("/login", {"username": "x", "password": "y"})[0] == 404


def test_no_response_from_the_listener_carries_the_token(app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    token = _enable(factory)
    client = _remote(monkeypatch, factory)
    bodies = [client.get("/")[2], client.get("/mcp")[2], _list_tools(client, {})[2],
              _list_tools(client, {"X-Token": WRONG})[2],
              _list_tools(client, {"X-Token": token})[2]]
    for body in bodies:
        assert token not in body


# ── Сторож порту ────────────────────────────────────────────────────────────


def test_the_worker_builds_its_config_with_log_config_none(app_db, monkeypatch):  # noqa: F811
    """Прод без консолі: стандартний конфіг логів uvicorn падає на
    `sys.stdout.isatty()` ще в конструкторі Config, і слухач не відкриває порт
    (так мовчало табло печей 0.15.5). Тому аргумент стережемо тестом."""
    import uvicorn

    from app.routers.mcp import create_mcp_app

    _, factory = app_db
    _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(mg, "_START_DELAY_SECONDS", 0)
    stop = threading.Event()
    seen: list[dict] = []

    class FakeConfig:
        def __init__(self, app, **kwargs):
            seen.append(kwargs)
            # Конфіг зібрано — далі сторожу робити нічого, цикл мусить вийти.
            stop.set()
            raise RuntimeError("далі не йдемо — тест перевіряє лише аргументи")

    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    mg.gateway_worker(stop, create_mcp_app)

    assert seen, "сторож навіть не спробував підняти сервер"
    kwargs = seen[0]
    assert kwargs["log_config"] is None, "без цього прод без консолі не відкриє порт"
    assert kwargs["host"] == "0.0.0.0", "сенс слухача — запит з іншої машини"
    assert kwargs["port"] == mg.GATEWAY_PORT
    assert kwargs["lifespan"] == "off" and kwargs["access_log"] is False
    assert kwargs["log_level"] == "warning"
    assert not mg.status_snapshot().listening


def test_the_worker_does_not_open_the_port_without_a_token(app_db, monkeypatch):  # noqa: F811
    """«Увімкнено» без токена — не слухаємо: порт у мережі без межі доступу
    гірший за вимкнений."""
    import uvicorn

    from app.routers.mcp import create_mcp_app
    from app.settings_store import set_setting

    _, factory = app_db
    with factory() as db:
        set_setting(db, mg.ENABLED_KEY, "1")
        set_setting(db, mg.TOKEN_KEY, "")
        db.commit()
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(mg, "_START_DELAY_SECONDS", 0)
    tried: list[dict] = []
    monkeypatch.setattr(uvicorn, "Config", lambda app, **kw: tried.append(kw))
    stop = threading.Event()

    def run():
        mg.gateway_worker(stop, create_mcp_app)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    stop.set()
    worker.join(timeout=10)
    assert not tried, "без токена слухач не піднімається"


@pytest.mark.parametrize("no_console", [False, True], ids=["console", "frozen-no-console"])
def test_switch_really_opens_and_closes_the_port(app_db, monkeypatch, no_console):  # noqa: F811
    """Перехід, а не стан, і на справжньому сокеті: увімкнули — слухач відповідає
    по HTTP, вимкнули — порт закрився.

    `frozen-no-console` — умови встановленого KuubMill.exe (`console=False`):
    `sys.stdout`/`sys.stderr` = None. Це єдина перевірка, де `log_config=None`
    доводиться наслідком, а не аргументом; у 0.15.5 саме тут табло печей не
    відкрило порт жодного разу, а тест із консоллю був зелений."""
    import socket
    import sys
    import time
    import urllib.error
    import urllib.request

    from app.routers.mcp import create_mcp_app
    from app.settings_store import set_setting

    if no_console:
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
    _, factory = app_db
    token = _enable(factory)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(mg, "GATEWAY_PORT", port)
    monkeypatch.setattr(mg, "_START_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    stop = threading.Event()
    worker = threading.Thread(target=mg.gateway_worker, args=(stop, create_mcp_app), daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{port}/mcp"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()

    def _ask(bearer: str | None):
        headers = {"Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(url, data=body, headers=headers)
        return urllib.request.urlopen(request, timeout=2).read().decode()

    def _wait(fn, timeout=20.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.3)
        pytest.fail(f"не дочекались: {last!r}")

    try:
        answer = _wait(lambda: _ask(token))
        assert "kmill_queue" in answer
        assert mg.status_snapshot().listening
        try:
            _ask(None)
            pytest.fail("без токена слухач відповів 200")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert token not in exc.read().decode()

        with factory() as db:
            set_setting(db, mg.ENABLED_KEY, "")
            db.commit()
        _wait(lambda: _closed(url))
        _wait(lambda: _not_listening())
    finally:
        stop.set()
        worker.join(timeout=15)


def _closed(url: str) -> bool:
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}"), timeout=1)
    except urllib.error.HTTPError:
        raise AssertionError("порт досі відповідає")
    except (urllib.error.URLError, ConnectionError, OSError):
        return True
    raise AssertionError("порт досі відповідає")


def _not_listening() -> bool:
    assert not mg.status_snapshot().listening
    return True


def _json(body: str) -> dict:
    return json.loads(body)
