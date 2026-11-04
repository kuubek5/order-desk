"""Піч змінила IP — знайти її за MAC і оновити адресу (власник 04.11.26)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.furnace_ocr import STATUS_UNKNOWN, PanelReading
from app.models import Furnace, FurnaceReading, TelegramWatch
from app.services import furnace, furnace_rediscover as fr
from conftest import make_memory_engine

MAC = "aa-bb-cc-dd-ee-01"
OLD = "192.168.1.61"
NEW = "192.168.1.77"


@pytest.fixture(autouse=True)
def _clean():
    fr.reset_for_tests()
    furnace.reset_state_for_tests()
    yield
    fr.reset_for_tests()
    furnace.reset_state_for_tests()


@pytest.fixture
def db():
    with Session(make_memory_engine(), expire_on_commit=False) as session:
        yield session


def _furnace(db, host=OLD, mac=None, name="Бочка") -> Furnace:
    row = Furnace(name=name, host=host, port=5900, created_at=datetime(2026, 11, 4), mac_address=mac)
    db.add(row)
    db.commit()
    return row


def _poll(row: Furnace, *, error=None, reading=None, at=None) -> None:
    target = furnace.target_of(row)
    with furnace._states_lock:
        state = furnace._states.setdefault(target.key, furnace.FurnaceState(target=target))
        state.attempted_at = at or datetime.now()
        state.error = error
        state.reading = reading


def _no_start(*_args, **_kwargs):
    raise AssertionError("пошук не мав стартувати")


ARP_OUTPUT = """
Интерфейс: 192.168.1.20 --- 0x5
  адрес в Интернете      Физический адрес      Тип
  192.168.1.1           d4-01-c3-11-22-33     динамический
  192.168.1.77          AA-BB-CC-DD-EE-01     динамический
  192.168.1.255         ff-ff-ff-ff-ff-ff     статический
  224.0.0.22            01-00-5e-00-00-16     статический
"""


def test_parse_arp_normalises_and_drops_broadcast():
    assert fr.parse_arp(ARP_OUTPUT) == {"192.168.1.1": "d4-01-c3-11-22-33", NEW: MAC}


# ── Пошук ───────────────────────────────────────────────────────────────────


def test_known_mac_finds_new_address_without_any_vnc():
    found, mac, verdict = fr.search(
        OLD, 5900, MAC, "pw", set(),
        is_open=lambda h, p: False,
        arp=lambda: {NEW: MAC, OLD: "11-22-33-44-55-66"},
        is_furnace=lambda *a: pytest.fail("MAC відомий — кадри не потрібні"),
    )
    assert (found, mac) == (NEW, MAC)
    assert NEW in verdict


def test_known_mac_on_same_address_means_vnc_is_down():
    found, _mac, verdict = fr.search(
        OLD, 5900, MAC, "pw", set(), is_open=lambda h, p: False, arp=lambda: {OLD: MAC},
    )
    assert found is None
    assert "VNC" in verdict and OLD in verdict


def test_known_mac_absent_means_furnace_off():
    found, _mac, verdict = fr.search(
        OLD, 5900, MAC, "pw", set(), is_open=lambda h, p: False, arp=lambda: {},
    )
    assert found is None
    assert "вимкнена" in verdict


def test_first_time_finds_single_panel_and_learns_mac():
    checked: list[str] = []

    def is_furnace(host, port, password):
        checked.append(host)
        assert password == "pw"
        return host == NEW

    found, mac, _ = fr.search(
        OLD, 5900, None, "pw", set(),
        is_open=lambda h, p: h in (NEW, "192.168.1.90", OLD),
        arp=lambda: {NEW: MAC},
        is_furnace=is_furnace,
    )
    assert (found, mac) == (NEW, MAC)
    # Стара адреса вже не відповідає як піч; її кадр нічого не доведе.
    assert sorted(checked) == [NEW, "192.168.1.90"]


def test_two_panels_are_not_guessed():
    found, _mac, verdict = fr.search(
        OLD, 5900, None, "pw", set(),
        is_open=lambda h, p: h in (NEW, "192.168.1.90"),
        arp=lambda: {},
        is_furnace=lambda *a: True,
    )
    assert found is None
    assert "не вгадуємо" in verdict


def test_other_furnace_address_is_not_a_candidate():
    found, _mac, verdict = fr.search(
        OLD, 5900, None, "pw", {NEW},
        is_open=lambda h, p: h == NEW,
        arp=lambda: {},
        is_furnace=lambda *a: True,
    )
    assert found is None
    assert "немає іншої адреси" in verdict


def test_hostname_is_not_searched():
    found, _mac, verdict = fr.search(
        "furnace-pc", 5900, None, "pw", set(), is_open=lambda h, p: True, arp=lambda: {},
    )
    assert found is None
    assert "не приватна" in verdict


# ── Прохід воркера ──────────────────────────────────────────────────────────


def test_silence_starts_one_search_after_threshold_then_waits(db):
    row = _furnace(db, mac=MAC)
    started: list[tuple] = []
    now = {"t": 1000.0}

    def tick_once(second: int) -> None:
        _poll(row, error="недоступна", at=datetime(2026, 11, 4, 12, 0, second))
        fr.tick(db, clock=lambda: now["t"], arp=dict, start=lambda *a: started.append(a))

    for second in range(fr.REDISCOVER_AFTER_FAILURES - 1):
        tick_once(second)
    assert started == []
    tick_once(fr.REDISCOVER_AFTER_FAILURES)
    assert len(started) == 1
    assert started[0][2:5] == (OLD, 5900, MAC)

    fr._running.clear()
    now["t"] += 60
    tick_once(40)
    assert len(started) == 1, "не частіше разу на 10 хв"
    now["t"] += fr.REDISCOVER_INTERVAL_SECONDS
    tick_once(41)
    assert len(started) == 2


def test_repeated_tick_without_new_attempt_does_not_count(db):
    row = _furnace(db)
    _poll(row, error="недоступна", at=datetime(2026, 11, 4, 12, 0, 0))
    for _ in range(fr.REDISCOVER_AFTER_FAILURES + 3):
        fr.tick(db, arp=dict, start=_no_start)


def test_recovery_resets_the_streak(db):
    row = _furnace(db)
    for second in range(fr.REDISCOVER_AFTER_FAILURES - 1):
        _poll(row, error="недоступна", at=datetime(2026, 11, 4, 12, 0, second))
        fr.tick(db, arp=dict, start=_no_start)
    _poll(row, reading=PanelReading(status="RUN"), at=datetime(2026, 11, 4, 12, 1, 0))
    fr.tick(db, arp=dict, start=_no_start)
    _poll(row, error="недоступна", at=datetime(2026, 11, 4, 12, 1, 6))
    fr.tick(db, arp=dict, start=_no_start)


def test_reading_furnace_learns_mac_once_an_hour(db):
    row = _furnace(db)
    calls: list[int] = []
    now = {"t": 1000.0}

    def arp():
        calls.append(1)
        return {OLD: MAC}

    _poll(row, reading=PanelReading(status="WAIT"))
    fr.tick(db, clock=lambda: now["t"], arp=arp, start=_no_start)
    assert row.mac_address == MAC
    fr.tick(db, clock=lambda: now["t"] + 10, arp=arp, start=_no_start)
    assert len(calls) == 1

    # Карту замінили: через годину новий MAC.
    now["t"] += fr.MAC_CHECK_INTERVAL_SECONDS + 1
    fr.tick(db, clock=lambda: now["t"], arp=lambda: {OLD: "11-11-11-11-11-11"}, start=_no_start)
    assert row.mac_address == "11-11-11-11-11-11"


def test_unrecognised_screen_teaches_no_mac(db):
    """Кадр є, а табло не впізнано — на адресі може бути чужий VNC."""
    row = _furnace(db)
    _poll(row, reading=PanelReading(status=STATUS_UNKNOWN))
    fr.tick(db, arp=lambda: {OLD: MAC}, start=_no_start)
    assert row.mac_address is None


# ── Запис знахідки ──────────────────────────────────────────────────────────


def test_found_address_moves_host_history_and_telegram_memory(db):
    row = _furnace(db)
    db.add(FurnaceReading(host=OLD, captured_at=datetime(2026, 11, 3), status="RUN"))
    db.add(TelegramWatch(key=f"furnace:{OLD}", state="RUN", since_at=datetime(2026, 11, 3),
                         seen_at=datetime(2026, 11, 3)))
    db.commit()
    _poll(row, error="недоступна")
    fr._found[row.id] = (OLD, NEW, MAC)

    fr.tick(db, arp=dict, start=_no_start)

    assert row.host == NEW and row.mac_address == MAC
    assert db.query(FurnaceReading).one().host == NEW
    assert db.get(TelegramWatch, f"furnace:{NEW}") is not None
    assert db.get(TelegramWatch, f"furnace:{OLD}") is None
    assert OLD not in furnace.states_snapshot()


def test_hand_edited_address_wins_over_found(db):
    row = _furnace(db, host="192.168.1.70")
    fr._found[row.id] = (OLD, NEW, MAC)
    fr.tick(db, arp=dict, start=_no_start)
    assert row.host == "192.168.1.70" and row.mac_address is None


def test_address_of_another_furnace_is_not_taken(db):
    row = _furnace(db)
    other = _furnace(db, host=NEW, name="Друга")
    fr._found[row.id] = (OLD, NEW, MAC)
    fr.tick(db, arp=dict, start=_no_start)
    assert row.host == OLD and other.host == NEW


def test_search_thread_reports_verdict(db, monkeypatch):
    monkeypatch.setattr(fr, "search", lambda *a, **k: (None, MAC, "піч вимкнена"))
    row = _furnace(db)
    thread_started = []
    real_thread = fr.threading.Thread

    def capture_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        thread_started.append(thread)
        return thread

    monkeypatch.setattr(fr.threading, "Thread", capture_thread)
    with fr._lock:
        fr._running.add(row.id)
    fr._start_search(row.id, row.name, OLD, 5900, MAC, "pw", set())
    thread_started[0].join(5)
    assert fr.last_verdict(row.id) == "піч вимкнена"
    assert row.id not in fr._running
