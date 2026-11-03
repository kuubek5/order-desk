"""Швидка перевірка нових листів між повними синками пошти (03.11.26).

Власник: «в ukr.net лист видно одразу, у нас — лише що щось прийшло». Повний
синк іде раз на 2 хв; між ними `InboxProbe` питає лише UIDNEXT Вхідних і,
коли той зрушив, будить синк одразу. Тут — що проба не будить синк дарма,
не губить лист через обрив і не логіниться на кожен запит.
"""

from __future__ import annotations

import contextlib
import logging

import pytest

from app import web
from app.services import mail_probe
from app.services.mail_probe import InboxProbe


class _Folder:
    def __init__(self, box):
        self.box = box

    def status(self, folder, options):
        assert folder == "INBOX"
        assert set(options) == {"UIDNEXT", "UIDVALIDITY"}
        if self.box.fail:
            raise OSError("connection reset")
        return {"UIDNEXT": self.box.server.uidnext, "UIDVALIDITY": self.box.server.uidvalidity}


class _Box:
    def __init__(self, server):
        self.server = server
        self.fail = False
        self.logged_out = False
        self.folder = _Folder(self)

    def logout(self):
        self.logged_out = True


class _Server:
    def __init__(self):
        self.uidnext = 100
        self.uidvalidity = 7
        self.logins: list[str] = []
        self.boxes: list[_Box] = []
        self.refuse = False

    def connect(self, login, password):
        if self.refuse:
            raise OSError("imap.ukr.net недоступний")
        self.logins.append(login)
        box = _Box(self)
        self.boxes.append(box)
        return box


@pytest.fixture(autouse=True)
def _fresh_throttle():
    mail_probe.log_throttle.clear(mail_probe._ERROR_LOG_KEY)
    yield
    mail_probe.log_throttle.clear(mail_probe._ERROR_LOG_KEY)


def test_new_letter_wakes_the_sync_once():
    server = _Server()
    probe = InboxProbe(connect=server.connect)
    assert probe.changed("lab", "pw") is False, "перша проба лише запам'ятовує стан"
    assert probe.changed("lab", "pw") is False
    server.uidnext = 101  # прийшов лист
    assert probe.changed("lab", "pw") is True
    assert probe.changed("lab", "pw") is False, "той самий лист вдруге синк не будить"


def test_one_login_for_many_probes():
    """Логін на кожну пробу — тисячі входів на добу; сервер може прийняти
    таке за підбір пароля. З'єднання одне, доки живе."""
    server = _Server()
    probe = InboxProbe(connect=server.connect)
    for _ in range(50):
        probe.changed("lab", "pw")
    assert server.logins == ["lab"]


def test_a_letter_that_arrived_during_an_outage_is_not_lost(caplog):
    server = _Server()
    probe = InboxProbe(connect=server.connect)
    probe.changed("lab", "pw")
    server.boxes[0].fail = True
    with caplog.at_level(logging.WARNING, logger="app.services.mail_probe"):
        assert probe.changed("lab", "pw") is False, "помилка — це «не знаю», а не «є лист»"
    assert server.boxes[0].logged_out, "мертве з'єднання закривається"
    assert "звичайним синком" in caplog.text

    server.uidnext = 103  # поки з'єднання не було, прийшло два листи
    assert probe.changed("lab", "pw") is True, "після перепідключення лист помічено"
    assert len(server.logins) == 2


def test_unreachable_server_is_logged_once_not_every_probe(caplog):
    server = _Server()
    server.refuse = True
    probe = InboxProbe(connect=server.connect)
    with caplog.at_level(logging.WARNING, logger="app.services.mail_probe"):
        for _ in range(5):
            assert probe.changed("lab", "pw") is False
    assert caplog.text.count("швидка перевірка") == 1


def test_another_mailbox_starts_from_scratch():
    """Змінили скриньку в налаштуваннях — UIDNEXT старої з новою не порівнюємо."""
    server = _Server()
    probe = InboxProbe(connect=server.connect)
    probe.changed("lab", "pw")
    server.uidnext = 5000
    assert probe.changed("other", "pw") is False
    assert server.boxes[0].logged_out
    assert server.logins == ["lab", "other"]


def test_uidvalidity_change_also_counts():
    server = _Server()
    probe = InboxProbe(connect=server.connect)
    probe.changed("lab", "pw")
    server.uidvalidity = 8
    assert probe.changed("lab", "pw") is True


# ── Цикл синку: проба будить синк раніше, але розклад лишається ─────────────

class _FakeStop:
    def __init__(self, clock):
        self.clock = clock
        self.waits: list[float] = []

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock[0] += seconds
        return False


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(web, "monotonic", lambda: now[0])
    monkeypatch.setattr(web, "MAIL_SYNC_INTERVAL_SECONDS", 120)
    monkeypatch.setattr(web, "MAIL_PROBE_INTERVAL_SECONDS", 20)
    return now


def test_quiet_mailbox_keeps_the_two_minute_schedule(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "_mail_probe_says_new", lambda probe: calls.append(1) or False)
    stop = _FakeStop(clock)
    web._wait_for_next_mail_sync(stop, probe=None)
    assert clock[0] == 120
    assert stop.waits == [20, 20, 20, 20, 20, 20]
    assert len(calls) == 5, "після останнього очікування вже повний синк — проба зайва"


def test_new_letter_cuts_the_wait_short(clock, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(web, "_mail_probe_says_new", lambda probe: next(answers))
    stop = _FakeStop(clock)
    web._wait_for_next_mail_sync(stop, probe=None)
    assert clock[0] == 40


def test_shutdown_stops_waiting_at_once(clock, monkeypatch):
    monkeypatch.setattr(web, "_mail_probe_says_new", lambda probe: pytest.fail("проба після зупинки"))

    class _Stopped:
        def wait(self, seconds):
            return True

    web._wait_for_next_mail_sync(_Stopped(), probe=None)


def test_unconfigured_mail_never_touches_the_server(monkeypatch):
    server = _Server()
    probe = InboxProbe(connect=server.connect)
    monkeypatch.setattr(web, "SessionLocal", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(web, "_imap_configured", lambda db: False)
    assert web._mail_probe_says_new(probe) is False
    assert server.logins == []
