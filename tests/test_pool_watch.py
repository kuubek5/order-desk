"""Діагностика пулу з'єднань (інцидент 26.11.26: 18 хв «QueuePool limit»,
а лог не казав, хто тримає з'єднання)."""

from __future__ import annotations

import logging
import threading

from sqlalchemy import create_engine, text

from app import pool_watch


def _engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}", pool_size=2, max_overflow=2,
                           pool_timeout=5)
    pool_watch.install(engine)
    return engine


def _hold_on_share(engine, taken: threading.Event, release: threading.Event):
    """Як запит, що тримає з'єднання й висить на мережевій шарі."""
    with engine.connect() as conn:
        conn.execute(text("select 1"))
        taken.set()
        release.wait(10)


def test_near_exhaustion_logs_where_holders_hang(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pool_watch, "ALERT_AT", 3)
    monkeypatch.setattr(pool_watch, "MIN_HELD_SECONDS", 0.0)
    pool_watch.reset_for_tests()
    engine = _engine(tmp_path)
    release = threading.Event()
    threads = []
    for _ in range(2):
        taken = threading.Event()
        t = threading.Thread(target=_hold_on_share, args=(engine, taken, release), name="holder")
        t.start()
        assert taken.wait(5)
        threads.append(t)
    try:
        with caplog.at_level(logging.WARNING, logger="app.pool_watch"):
            with engine.connect() as conn:     # третє — поріг
                conn.execute(text("select 1"))
    finally:
        release.set()
        for t in threads:
            t.join(5)
        engine.dispose()
    report = [r.getMessage() for r in caplog.records if "майже вичерпано" in r.getMessage()]
    assert len(report) == 1
    assert "зайнято 3" in report[0]
    assert "2× holder" in report[0], "однакові тримачі злиті з лічильником"
    assert "_hold_on_share" in report[0], "видно, де саме висить тримач"


def test_below_threshold_is_silent_and_released_connections_are_forgotten(tmp_path, caplog):
    pool_watch.reset_for_tests()
    engine = _engine(tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.pool_watch"):
        for _ in range(5):
            with engine.connect() as conn:
                conn.execute(text("select 1"))
    engine.dispose()
    assert not [r for r in caplog.records if r.name == "app.pool_watch"]
    assert pool_watch.holders_report() == []


def test_report_is_throttled(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pool_watch, "ALERT_AT", 1)
    pool_watch.reset_for_tests()
    engine = _engine(tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.pool_watch"):
        for _ in range(4):
            with engine.connect() as conn:
                conn.execute(text("select 1"))
    engine.dispose()
    assert len([r for r in caplog.records if r.name == "app.pool_watch"]) == 1


def test_app_engine_has_the_watch_installed():
    from sqlalchemy import event

    from app.db import engine

    assert event.contains(engine, "checkin", pool_watch._on_checkin)
