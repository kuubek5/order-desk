"""Верстат змінив IP — пошук агента в мережі цеху (28.10.26, 250i-Sec .58 → .113).

Стережемо:
1. мовчить досить довго → пошук іде, знахідка лягає в `Machine.host` наступним
   тіком, відкритий обрив старої адреси закривається з поясненням;
2. мовчить мало, VNC-верстат, верстат на зв'язку → пошуку немає;
3. не частіше за інтервал;
4. адресу займає інший верстат або її вже змінили руками → не чіпаємо;
5. обходимо лише приватну /24, без старої адреси й без живих верстатів.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.crypto import encrypt_value
from app.db import Base
from app.models import Machine, MachineLinkEvent
from app.services import machine_rediscover as mr
from app.services import machines


@pytest.fixture(autouse=True)
def _clean():
    mr.reset_for_tests()
    machines.reset_state_for_tests()
    yield
    mr.reset_for_tests()
    machines.reset_state_for_tests()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


NOW = datetime(2026, 10, 28, 9, 30)


def _machine(db, name, host, *, token="tok-" , enabled=True) -> Machine:
    m = Machine(name=name, host=host, port=8765, enabled=enabled, created_at=NOW,
                agent_token_encrypted=encrypt_value(token + name) if token else None)
    db.add(m)
    db.commit()
    return m


def _silent(db, machine: Machine, streak: int) -> None:
    """Стан «мовчить N тіків» — як його лишає poll_target."""
    target = machines.target_of(machine)
    with machines._states_lock:
        machines._states[target.key] = machines.MachineState(target=target, fail_streak=streak)


def _alive(db, machine: Machine) -> None:
    target = machines.target_of(machine)
    with machines._states_lock:
        machines._states[target.key] = machines.MachineState(
            target=target, fail_streak=0, last_ok_at=NOW
        )


def _wait_scans():
    for _ in range(200):
        with mr._lock:
            if not mr._running:
                return
        time.sleep(0.01)
    raise AssertionError("пошук не завершився")


class FakeNet:
    """Мережа цеху: на якій адресі сидить агент із яким токеном."""

    def __init__(self, agents: dict[str, str]):
        self.agents = agents
        self.asked: list[str] = []

    def __call__(self, host: str, port: int, token: str) -> bool:
        self.asked.append(host)
        return self.agents.get(host) == token


def test_moved_machine_is_found_and_address_updated(db):
    sec = _machine(db, "250i-Sec", "192.168.1.58")
    tolik = _machine(db, "250i-Tolik", "192.168.1.81")
    db.add(MachineLinkEvent(host="192.168.1.58-8765", name="250i-Sec", started_at=NOW,
                            detected_at=NOW, error="мовчить", cause="silent"))
    db.commit()
    _silent(db, sec, mr.REDISCOVER_AFTER_FAILURES)
    _alive(db, tolik)
    net = FakeNet({"192.168.1.113": "tok-250i-Sec", "192.168.1.81": "tok-250i-Tolik"})

    mr.tick(db, now=NOW, clock=lambda: 1000.0, probe=net)
    _wait_scans()
    # Живий сусід і стара адреса не опитуються.
    assert "192.168.1.81" not in net.asked
    assert "192.168.1.58" not in net.asked
    assert db.get(Machine, sec.id).host == "192.168.1.58", "пише лише наступний тік воркера"

    mr.tick(db, now=NOW, clock=lambda: 1005.0, probe=net)
    db.expire_all()
    assert db.get(Machine, sec.id).host == "192.168.1.113"
    assert db.get(Machine, sec.id).port == 8765
    assert db.get(Machine, tolik.id).host == "192.168.1.81"
    event = db.scalars(select(MachineLinkEvent)).one()
    assert event.ended_at == NOW
    assert "192.168.1.113" in (event.probe_verdict or "")


def test_no_scan_for_short_silence_vnc_or_alive(db):
    short = _machine(db, "short", "192.168.1.10")
    vnc = _machine(db, "vnc", "192.168.1.11", token=None)
    ok = _machine(db, "ok", "192.168.1.12")
    _silent(db, short, mr.REDISCOVER_AFTER_FAILURES - 1)
    _silent(db, vnc, 500)
    _alive(db, ok)
    net = FakeNet({})
    mr.tick(db, now=NOW, clock=lambda: 1000.0, probe=net)
    _wait_scans()
    assert net.asked == []


def test_not_more_often_than_interval(db):
    sec = _machine(db, "250i-Sec", "192.168.1.58")
    _silent(db, sec, 100)
    net = FakeNet({})
    mr.tick(db, now=NOW, clock=lambda: 1000.0, probe=net)
    _wait_scans()
    first = len(net.asked)
    assert first == 253, "уся /24 без старої адреси"
    mr.tick(db, now=NOW, clock=lambda: 1000.0 + mr.REDISCOVER_INTERVAL_SECONDS - 1, probe=net)
    _wait_scans()
    assert len(net.asked) == first
    mr.tick(db, now=NOW, clock=lambda: 1000.0 + mr.REDISCOVER_INTERVAL_SECONDS + 1, probe=net)
    _wait_scans()
    assert len(net.asked) == 2 * first


def test_address_taken_by_another_machine_is_left_alone(db):
    sec = _machine(db, "250i-Sec", "192.168.1.58")
    pmma = _machine(db, "250-pmma", "192.168.1.78")   # теж мовчить, тримає .78
    _silent(db, sec, 100)
    _silent(db, pmma, 5)
    net = FakeNet({"192.168.1.78": "tok-250i-Sec"})   # ПК Sec отримав стару адресу pmma
    mr.tick(db, now=NOW, clock=lambda: 1000.0, probe=net)
    _wait_scans()
    mr.tick(db, now=NOW, clock=lambda: 1005.0, probe=net)
    db.expire_all()
    assert db.get(Machine, sec.id).host == "192.168.1.58"
    assert db.get(Machine, pmma.id).host == "192.168.1.78"


def test_manual_edit_meanwhile_wins(db):
    sec = _machine(db, "250i-Sec", "192.168.1.58")
    _silent(db, sec, 100)
    net = FakeNet({"192.168.1.113": "tok-250i-Sec"})
    mr.tick(db, now=NOW, clock=lambda: 1000.0, probe=net)
    _wait_scans()
    sec.host = "192.168.1.200"   # людина встигла вписати свою адресу
    db.commit()
    mr.tick(db, now=NOW, clock=lambda: 1005.0, probe=net)
    db.expire_all()
    assert db.get(Machine, sec.id).host == "192.168.1.200"


def test_candidates_only_private_ipv4():
    assert mr.candidate_hosts("pc-sec.local", set()) == []
    assert mr.candidate_hosts("8.8.8.8", set()) == []
    hosts = mr.candidate_hosts("192.168.1.58", {"192.168.1.58", "192.168.1.81"})
    assert len(hosts) == 252 and "192.168.1.1" in hosts and "192.168.1.254" in hosts
    assert "192.168.1.58" not in hosts and "192.168.1.0" not in hosts and "192.168.1.255" not in hosts


def test_real_probe_needs_200_with_hostname(monkeypatch):
    import requests

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body = code, body

        def json(self):
            return self._body

    calls = {}

    def fake_get(url, headers=None, timeout=None):
        calls["url"], calls["token"] = url, headers.get("X-Agent-Token")
        return responses.pop(0)

    monkeypatch.setattr(requests, "get", fake_get)
    responses = [Resp(200, {"hostname": "SEC-PC"}), Resp(403, {}), Resp(200, {"x": 1})]
    assert mr.agent_answers("192.168.1.113", 8765, "tok") is True
    assert calls == {"url": "http://192.168.1.113:8765/status", "token": "tok"}
    assert mr.agent_answers("192.168.1.114", 8765, "tok") is False
    assert mr.agent_answers("192.168.1.115", 8765, "tok") is False
