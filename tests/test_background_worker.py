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
