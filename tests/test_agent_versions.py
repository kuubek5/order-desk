"""Версія агента на верстаті: `app/services/agent_versions.py`.

KuubMill питає `GET /status` агента окремим рідким проходом і памʼятає відповідь.
Мережі в тестах немає: `refresh_once` приймає підмінний `fetch`.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.crypto import encrypt_value
from app.models import Base, Machine
from app.services import agent_versions as av
from app.services import device_diag


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


def _machine(db: Session, name: str, host: str, *, token: str | None = "t", enabled: bool = True, version: str = "") -> Machine:
    machine = Machine(
        name=name,
        host=host,
        port=8765,
        enabled=enabled,
        agent_token_encrypted=encrypt_value(token) if token else None,
        agent_version=version,
        created_at=datetime(2026, 11, 1),
    )
    db.add(machine)
    db.commit()
    return machine


def test_build_date_and_newest():
    assert av.build_date("2026.11.16-abc1234") == (2026, 11, 16)
    assert av.build_date("0.1.0") is None
    assert av.build_date("dev") is None
    assert av.build_date("") is None
    assert av.newest(["0.1.0", "2026.09.04-aaaaaaa", "2026.11.16-bbbbbbb"]) == "2026.11.16-bbbbbbb"
    assert av.newest(["0.1.0", "dev"]) == ""


def test_outdated_compares_build_dates_and_never_guesses():
    fleet = ["2026.11.16-bbbbbbb", "2026.09.04-aaaaaaa", "0.1.0"]
    assert av.is_outdated("2026.09.04-aaaaaaa", fleet)
    assert av.is_outdated("0.1.0", fleet)
    assert not av.is_outdated("2026.11.16-bbbbbbb", fleet)
    # Інший білд того самого дня: порядок з рядка не видно — старим не називаємо.
    assert not av.is_outdated("2026.11.16-ccccccc", fleet)
    # Невідома версія й цех без датованих білдів — без позначки.
    assert not av.is_outdated("", fleet)
    assert not av.is_outdated("0.1.0", ["0.1.0", "dev"])


def test_refresh_writes_versions_and_keeps_the_old_one_on_silence(db):
    tolik = _machine(db, "Tolik", "192.168.1.10")
    sec = _machine(db, "Sec", "192.168.1.11", version="2026.09.04-aaaaaaa")
    vnc = _machine(db, "Old VNC", "192.168.1.12", token=None)
    off = _machine(db, "Repair", "192.168.1.13", enabled=False)
    asked: list[tuple[str, int, str]] = []

    def fetch(host: str, port: int, token: str):
        asked.append((host, port, token))
        return "2026.11.16-bbbbbbb" if host.endswith(".10") else None

    moment = datetime(2026, 11, 17, 12, 0)
    result = av.refresh_once(db, fetch=fetch, now=moment)

    assert result == {"asked": 2, "answered": 1, "changed": 1}
    assert sorted(asked) == [("192.168.1.10", 8765, "t"), ("192.168.1.11", 8765, "t")]
    db.expire_all()
    assert (db.get(Machine, tolik.id).agent_version, db.get(Machine, tolik.id).agent_version_at) == ("2026.11.16-bbbbbbb", moment)
    # Не відповів — те, що знали, лишається, і час «бачили» не оновлюється.
    assert (db.get(Machine, sec.id).agent_version, db.get(Machine, sec.id).agent_version_at) == ("2026.09.04-aaaaaaa", None)
    assert db.get(Machine, vnc.id).agent_version == ""
    assert db.get(Machine, off.id).agent_version == ""


def test_refresh_without_agents_does_not_touch_the_network(db):
    _machine(db, "Old VNC", "192.168.1.12", token=None)

    def fetch(host: str, port: int, token: str):  # pragma: no cover - не мусить кликатись
        raise AssertionError("мережу не чіпати")

    assert av.refresh_once(db, fetch=fetch) == {"asked": 0, "answered": 0, "changed": 0}


def test_fetch_version_reads_status_and_treats_everything_else_as_unknown(monkeypatch):
    import requests

    class Resp:
        def __init__(self, status: int, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return answers.pop(0)

    monkeypatch.setattr(requests, "get", fake_get)
    answers = [
        Resp(200, {"version": " 2026.11.16-bbbbbbb ", "hostname": "pc"}),
        Resp(403, {"version": "x"}),
        Resp(200, ValueError("not json")),
        Resp(200, {"hostname": "pc"}),
        Resp(200, ["x"]),
    ]
    assert av.fetch_version("10.0.0.5", 8765, "secret") == "2026.11.16-bbbbbbb"
    assert calls[0][0] == "http://10.0.0.5:8765/status"
    assert calls[0][1] == {"X-Agent-Token": "secret"}
    for _ in range(4):
        assert av.fetch_version("10.0.0.5", 8765, "secret") is None

    def boom(url, headers, timeout):
        raise requests.ConnectionError("silence")

    monkeypatch.setattr(requests, "get", boom)
    assert av.fetch_version("10.0.0.5", 8765, "secret") is None


def test_stale_ids_ignore_disabled_machines(db):
    fresh = _machine(db, "Tolik", "192.168.1.10", version="2026.11.16-bbbbbbb")
    old = _machine(db, "Sec", "192.168.1.11", version="0.1.0")
    silent = _machine(db, "Olejka", "192.168.1.14")
    parked = _machine(db, "Repair", "192.168.1.13", enabled=False, version="2027.01.01-ddddddd")
    rows = [fresh, old, silent, parked]
    assert av.stale_ids(rows) == {old.id}
    assert av.stale_ids([]) == set()


def test_devices_tool_reports_agent_version(db):
    _machine(db, "Tolik", "192.168.1.10", version="2026.11.16-bbbbbbb")
    _machine(db, "Sec", "192.168.1.11", version="0.1.0")
    _machine(db, "Olejka", "192.168.1.14")

    mills = {m["назва"]: m for m in device_diag.devices(db)["верстати"]}

    assert (mills["Tolik"]["версія_агента"], mills["Tolik"]["агент_застарів"]) == ("2026.11.16-bbbbbbb", False)
    assert (mills["Sec"]["версія_агента"], mills["Sec"]["агент_застарів"]) == ("0.1.0", True)
    assert (mills["Olejka"]["версія_агента"], mills["Olejka"]["агент_застарів"]) == (None, False)


def test_settings_row_shows_the_version_and_marks_the_old_agent(db):
    from app.routers.deps import templates

    _machine(db, "Tolik", "192.168.1.10", version="2026.11.16-bbbbbbb")
    _machine(db, "Sec", "192.168.1.11", version="0.1.0")
    _machine(db, "Olejka", "192.168.1.14")
    rows = list(db.query(Machine).order_by(Machine.id))

    html = templates.env.get_template("_settings_machines.html").render(machines=rows)

    assert html.count('class="mc-agent-ver mono') == 2  # Olejka ще не відповідав — рядка немає
    assert "агент 2026.11.16-bbbbbbb\n" in html
    assert 'class="mc-agent-ver mono is-stale"' in html
    assert "агент 0.1.0 · застарів" in html
