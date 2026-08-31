"""Живий індикатор синхронізації: детектор «іде зараз», фоновий імпорт історії
в daemon-потоці з одноразовим flash-результатом, і роут /sheets/state, що
перетворює цей flash на toast.

Фонові потоки тестуються через заглушку sync_google_sheets (жодної мережі й
жодної справжньої БД для самого імпорту)."""

import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.sheet_sync_service as sheet_sync
from app.db import Base
from app.models import User
from app.routers import queue as queue_router


def _db() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _admin(db: Session) -> User:
    user = User(username="root", password_hash="x", full_name="Роман", role="адмін")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None):
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


@pytest.fixture(autouse=True)
def _reset_import_state():
    """Модульні глобали фонового імпорту переживають тест — чистимо до і після,
    щоб черговість тестів не текла."""
    with sheet_sync._import_state_lock:
        sheet_sync._import_running = False
        sheet_sync._import_flash = None
    yield
    with sheet_sync._import_state_lock:
        sheet_sync._import_running = False
        sheet_sync._import_flash = None


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# --- running detector ---------------------------------------------------------


def test_is_sheet_sync_running_reflects_lock():
    assert sheet_sync.is_sheet_sync_running() is False
    acquired = sheet_sync._sync_lock.acquire(blocking=False)
    assert acquired
    try:
        assert sheet_sync.is_sheet_sync_running() is True
    finally:
        sheet_sync._sync_lock.release()
    assert sheet_sync.is_sheet_sync_running() is False


# --- background import job -----------------------------------------------------


def _stub_session_local(monkeypatch):
    @contextmanager
    def fake():
        yield object()

    monkeypatch.setattr(sheet_sync, "SessionLocal", fake)


def test_background_import_runs_and_sets_success_flash(monkeypatch):
    _stub_session_local(monkeypatch)
    monkeypatch.setattr(
        sheet_sync, "sync_google_sheets", lambda *a, **k: object()
    )
    monkeypatch.setattr(sheet_sync, "summary_message", lambda summary: "42 нових")

    assert sheet_sync.start_background_import() is True
    assert _wait_until(lambda: not sheet_sync.import_running())

    flash = sheet_sync.pop_import_flash()
    assert flash["kind"] == "success"
    assert "42 нових" in flash["message"]
    # one-shot — другий pop порожній
    assert sheet_sync.pop_import_flash() is None


def test_background_import_surfaces_sync_error(monkeypatch):
    _stub_session_local(monkeypatch)

    def boom(*a, **k):
        raise sheet_sync.SheetSyncError("немає доступу")

    monkeypatch.setattr(sheet_sync, "sync_google_sheets", boom)

    assert sheet_sync.start_background_import() is True
    assert _wait_until(lambda: not sheet_sync.import_running())

    flash = sheet_sync.pop_import_flash()
    assert flash["kind"] == "error"
    assert "немає доступу" in flash["message"]


def test_second_import_while_running_is_rejected(monkeypatch):
    _stub_session_local(monkeypatch)
    gate = threading.Event()

    def blocking(*a, **k):
        gate.wait(timeout=3.0)
        return object()

    monkeypatch.setattr(sheet_sync, "sync_google_sheets", blocking)
    monkeypatch.setattr(sheet_sync, "summary_message", lambda summary: "ok")

    assert sheet_sync.start_background_import() is True
    assert _wait_until(lambda: sheet_sync.import_running())
    # Другий клік поки перший ще працює — відмова, без другого потоку.
    assert sheet_sync.start_background_import() is False

    gate.set()
    assert _wait_until(lambda: not sheet_sync.import_running())
    assert sheet_sync.pop_import_flash()["kind"] == "success"


# --- import-history route: тепер стартує фон, не блокує запит ------------------


def test_import_history_route_starts_background_and_returns_fast(monkeypatch):
    db = _db()
    admin = _admin(db)
    called = {"n": 0}
    monkeypatch.setattr(
        queue_router, "start_background_import", lambda: called.__setitem__("n", called["n"] + 1) or True
    )
    # Синхронний sync_google_sheets НЕ має викликатись із запиту.
    monkeypatch.setattr(
        queue_router, "sync_google_sheets",
        lambda *a, **k: pytest.fail("import must run in background, not inline"),
    )

    resp = queue_router.import_sheet_history(_request(admin.id), db)
    assert resp.status_code == 303
    assert called["n"] == 1


# --- /sheets/state route: flash → toast --------------------------------------


def test_sheet_state_route_pops_flash_into_toast(monkeypatch):
    db = _db()
    admin = _admin(db)
    monkeypatch.setattr(queue_router, "sheets_configured", lambda s: True)
    monkeypatch.setattr(queue_router, "queue_sync_summary", lambda s: "Синхронізовано 09:00")
    monkeypatch.setattr(
        queue_router, "live_sync_status",
        lambda s: {"sheet": {"state": "success", "label": "✓", "running": False, "paused": False},
                   "mail": {"state": "success", "label": "✓", "running": False}},
    )
    # Черга має готовий одноразовий результат імпорту.
    with sheet_sync._import_state_lock:
        sheet_sync._import_flash = {"kind": "success", "message": "Історію імпортовано."}

    resp = queue_router.sheet_sync_state(_request(admin.id), db)
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert trigger["toast"]["message"] == "Історію імпортовано."
    assert trigger["toast"]["kind"] == "success"
    # flash з'їдений — наступний полл не повторить toast
    assert sheet_sync.pop_import_flash() is None


def test_sheet_state_route_no_flash_no_trigger(monkeypatch):
    db = _db()
    admin = _admin(db)
    monkeypatch.setattr(queue_router, "sheets_configured", lambda s: True)
    monkeypatch.setattr(queue_router, "queue_sync_summary", lambda s: "Синхронізовано 09:00")
    monkeypatch.setattr(
        queue_router, "live_sync_status",
        lambda s: {"sheet": {"state": "success", "label": "✓", "running": True, "paused": False},
                   "mail": {"state": "success", "label": "✓", "running": False}},
    )

    resp = queue_router.sheet_sync_state(_request(admin.id), db)
    assert "HX-Trigger" not in resp.headers


def test_sheet_state_route_logged_out_is_quiet():
    db = _db()
    resp = queue_router.sheet_sync_state(_request(None), db)
    assert resp.status_code == 204
