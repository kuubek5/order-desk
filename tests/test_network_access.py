"""«Робота з інших ПК» — гейт адреси, теки по мережі, перемикач з перезапуском.

Що ламається тихо саме тут:

* **Гейт.** Адмінська дія по мережі має працювати ЛИШЕ з увімкненим
  перемикачем і лише з приватної адреси. Якщо перевірка поїде в один бік —
  адмін з другого ПК бачить 403 без пояснення; в інший — секрети відкриті
  будь-кому, хто дістав до порту.
* **Тека по мережі.** Провідник сервера з іншого ПК не відкрити; роут мусить
  віддати ШЛЯХ, а не 403, інакше кнопка з іншого ПК — мовчазна.
* **Перемикач.** Адреса `uvicorn` береться раз на старті, тож роут мусить
  чесно сказати, чи перезапуск відбудеться, і зберегти налаштування в обох
  випадках.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import network_access
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

LAN = "192.168.1.55"
PUBLIC = "8.8.8.8"


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch):
    """Стан «що слухає» й «хто перезапускає» — глобали процесу; кожен тест
    починає з чистого."""
    monkeypatch.setattr(network_access, "_bound_host", None)
    monkeypatch.setattr(network_access, "_restarter", None)


def _request(host):
    client = None if host is None else SimpleNamespace(host=host)
    return SimpleNamespace(client=client, headers={})


def _set_enabled(session_factory, on: bool) -> None:
    from app.settings_store import set_setting

    with session_factory() as db:
        set_setting(db, network_access.ENABLED_KEY, "1" if on else "")
        db.commit()


# ── Гейт адреси ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "enabled", "expected"),
    [
        ("127.0.0.1", False, True),
        ("127.0.0.1", True, True),
        ("::1", False, True),
        (LAN, False, False),
        (LAN, True, True),
        ("10.8.0.2", True, True),  # тунель WireGuard — теж приватна
        (PUBLIC, True, False),
        (PUBLIC, False, False),
        (None, True, False),
        ("not-an-ip", True, False),
    ],
)
def test_trusted_request_matrix(app_db, host, enabled, expected):  # noqa: F811
    _, session_factory = app_db
    _set_enabled(session_factory, enabled)
    with session_factory() as db:
        assert network_access.is_trusted_request(_request(host), db) is expected


def test_admin_action_over_network_follows_the_switch(app_db):  # noqa: F811
    """Справжній роут за `require_settings_admin`: 403 з мережі, поки
    перемикач вимкнено; 303 (виконано) — коли ввімкнено."""
    app, session_factory = app_db
    client = MiniClient(app, client_host=LAN)
    status, _, _ = client.login(*ADMIN)
    assert status in (200, 302, 303), status

    status, _, body = client.post("/settings/mcp/token")
    assert status == 403, body
    assert "Робота з інших ПК" in body

    _set_enabled(session_factory, True)
    status, _, _ = client.post("/settings/mcp/token")
    assert status == 303

    # Публічна адреса не пускається навіть із перемикачем.
    stranger = MiniClient(app, client_host=PUBLIC)
    stranger.login(*ADMIN)
    status, _, _ = stranger.post("/settings/mcp/token")
    assert status == 403


# ── Тека по мережі ──────────────────────────────────────────────────────────


def test_open_folder_response_opens_locally_and_copies_over_network(app_db, monkeypatch, tmp_path):  # noqa: F811
    import json

    from app.routers import deps

    opened: list = []
    opener = opened.append
    _, session_factory = app_db

    with session_factory() as db:
        response = deps.open_folder_response(_request("127.0.0.1"), db, tmp_path, opener=opener, log_label="t")
        assert json.loads(response.body) == {"opened": True}
        assert opened == [tmp_path]

        # Мережа без перемикача — 403, як і було.
        with pytest.raises(Exception) as excinfo:
            deps.open_folder_response(_request(LAN), db, tmp_path, opener=opener, log_label="t")
        assert getattr(excinfo.value, "status_code", None) == 403

    _set_enabled(session_factory, True)
    with session_factory() as db:
        response = deps.open_folder_response(_request(LAN), db, tmp_path, opener=opener, log_label="t")
        payload = json.loads(response.body)
        assert payload == {"opened": False, "path": str(tmp_path)}
    # Провідник на сервері з мережі НЕ відкривався.
    assert opened == [tmp_path]


# ── Перемикач і перезапуск ──────────────────────────────────────────────────


def test_toggle_saves_and_restarts_when_launcher_registered(app_db):  # noqa: F811
    app, session_factory = app_db
    network_access.set_bound_host(network_access.LOOPBACK_HOST)
    calls: list[str] = []
    network_access.register_restarter(lambda: calls.append("restart"))

    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, body = client.post(
        "/settings/network/toggle", headers={"X-Requested-With": "fetch"}
    )
    assert status == 200, body
    import json

    payload = json.loads(body)
    assert payload["enabled"] is True
    assert payload["restarting"] is True
    assert calls == ["restart"]
    with session_factory() as db:
        assert network_access.access_enabled(db) is True
        assert network_access.restart_pending(db) is True


def test_toggle_without_launcher_saves_and_says_restart_by_hand(app_db):  # noqa: F811
    """dev під uvicorn: перезапускача немає й адреса не зареєстрована —
    налаштування зберігається, `restarting` чесно False."""
    app, session_factory = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, body = client.post(
        "/settings/network/toggle", headers={"X-Requested-With": "fetch"}
    )
    import json

    payload = json.loads(body)
    assert status == 200 and payload["restarting"] is False
    assert "перезапуск" in payload["message"].lower()
    with session_factory() as db:
        assert network_access.access_enabled(db) is True
        # Без зареєстрованої адреси «чекає перезапуску» не показується (dev).
        assert network_access.restart_pending(db) is False

    # Без fetch-заголовка — звичайний флеш + редирект, як у решти перемикачів.
    status, headers, _ = client.post("/settings/network/toggle")
    assert status == 303 and headers["location"].startswith("/settings#mcp")
    with session_factory() as db:
        assert network_access.access_enabled(db) is False


def test_no_restart_when_setting_already_matches_listener(app_db):  # noqa: F811
    """Слухач уже на 0.0.0.0, перемикач вимкнули й увімкнули назад —
    перезапускати нема чого."""
    app, session_factory = app_db
    _set_enabled(session_factory, True)
    network_access.set_bound_host(network_access.NETWORK_HOST)
    calls: list[str] = []
    network_access.register_restarter(lambda: calls.append("restart"))

    client = MiniClient(app)
    client.login(*ADMIN)
    client.post("/settings/network/toggle", headers={"X-Requested-With": "fetch"})  # off → pending
    assert calls == ["restart"]
    calls.clear()
    # Ще до перезапуску увімкнули назад: адреса збігається — перезапуск зайвий.
    import json

    _, _, body = client.post("/settings/network/toggle", headers={"X-Requested-With": "fetch"})
    assert json.loads(body)["restarting"] is False
    assert calls == []


# ── Дрібниці, які легко зіпсувати ───────────────────────────────────────────


def test_firewall_rule_is_private_profile_only():
    command = network_access.firewall_command()
    assert "localport=8000" in command
    assert "profile=private,domain" in command
    assert network_access.FIREWALL_RULE_NAME in network_access.firewall_remove_command()


def test_relaunch_command_waits_for_this_process_then_starts_exe():
    from app.windows_launcher import relaunch_command

    command = relaunch_command(4242, r"C:\Program Files\KuubMill\KuubMill.exe", [])
    script = command[-1]
    assert command[0] == "powershell" and "-Command" in command
    assert "Get-Process -Id 4242" in script
    assert "WaitForExit(60000)" in script
    assert "Start-Process -FilePath 'C:\\Program Files\\KuubMill\\KuubMill.exe'" in script
    assert "-ArgumentList" not in script

    with_args = relaunch_command(1, "python.exe", ["-m", "app.windows_launcher"])[-1]
    assert "-ArgumentList '-m' 'app.windows_launcher'" in with_args
