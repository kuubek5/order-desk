"""Супервізор фонових воркерів (_BackgroundWorker).

Крок 1 розбиття web.py: керування 5 фоновими потоками зведено з п'яти
однакових start/stop-блоків у lifespan до одного списку. Логіка самих
воркерів не мінялась — це лише впорядкування життєвого циклу. Тест фіксує
контракт: start піднімає живий daemon-потік, stop гасить його через
stop_event і приєднує.
"""

import threading
import time

from app.web import _BackgroundWorker


def test_start_runs_the_target_until_stop():
    ticks = []

    def target(stop_event: threading.Event):
        while not stop_event.wait(0.01):
            ticks.append(1)

    w = _BackgroundWorker("test-worker", target)
    w.start()
    assert w.thread is not None
    assert w.thread.daemon is True
    assert w.thread.is_alive()
    time.sleep(0.1)
    w.stop(timeout=1)
    assert not w.thread.is_alive(), "stop мусить зупинити потік"
    assert ticks, "воркер мав хоч раз тікнути до зупинки"


def test_stop_is_safe_before_start():
    # stop без start не має падати (напр. якщо старт колись обгорнуть умовою).
    _BackgroundWorker("never-started", lambda ev: None).stop(timeout=0.1)


# --- Вихід без stop() лишає слід (ревʼю 28.11.26) -----------------------------


def test_crashed_worker_is_logged_with_its_name(caplog):
    def target(stop_event):
        raise RuntimeError("шара відпала")

    w = _BackgroundWorker("test-crash", target)
    with caplog.at_level("INFO", logger="app.web"):
        w.start()
        w.thread.join(2)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "test-crash" in errors[0].getMessage()
    assert errors[0].exc_info is not None, "стек падіння в лозі"


def test_worker_that_returns_by_itself_leaves_an_info_line(caplog):
    w = _BackgroundWorker("test-return", lambda ev: None)
    with caplog.at_level("INFO", logger="app.web"):
        w.start()
        w.thread.join(2)
    lines = [r for r in caplog.records if "test-return" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelname == "INFO"


def test_normal_stop_is_silent(caplog):
    w = _BackgroundWorker("test-stop", lambda ev: ev.wait(5))
    with caplog.at_level("INFO", logger="app.web"):
        w.start()
        w.stop(timeout=2)
    assert not [r for r in caplog.records if "test-stop" in r.getMessage()]