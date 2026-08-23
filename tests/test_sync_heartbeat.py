"""Background sync heartbeat: the in-memory "is the loop alive right now"
signal for the queue sidebar (app/web.py SyncHeartbeat / _record_sync_heartbeat
/ _sync_heartbeat_status / _mail_sync_tick / _sheet_sync_tick).

SyncLog (see app/mail_sync_service.py, app/sheet_sync_service.py) can't answer
this question by itself: a healthy background tick that finds nothing new
writes no row, and a background-triggered failure writes no row either
(deliberately, to avoid spamming the audit table during an outage) — so
silence in SyncLog is ambiguous between "healthy and quiet" and "dead". This
heartbeat is updated on every tick regardless of outcome, closing that gap.
"""

from datetime import datetime, timedelta

import pytest

import app.web as web
from app.mail_sync_service import MailSyncBusyError, MailSyncError
from app.sheet_sync_service import SheetSyncBusyError, SheetSyncError
from app.web import SyncHeartbeat, _relative_time_uk, _sync_heartbeat_status


# ---------------------------------------------------------------------------
# _relative_time_uk — pure formatting, no globals involved.
# ---------------------------------------------------------------------------


def test_relative_time_under_a_minute_reads_just_now():
    now = datetime(2026, 8, 9, 12, 0, 0)
    reference = now - timedelta(seconds=30)
    assert _relative_time_uk(reference, now) == "щойно"


def test_relative_time_minutes_uses_ukrainian_pluralization():
    now = datetime(2026, 8, 9, 12, 0, 0)
    assert _relative_time_uk(now - timedelta(minutes=1), now) == "1 хвилину тому"
    assert _relative_time_uk(now - timedelta(minutes=3), now) == "3 хвилини тому"
    assert _relative_time_uk(now - timedelta(minutes=12), now) == "12 хвилин тому"


def test_relative_time_hours_uses_ukrainian_pluralization():
    now = datetime(2026, 8, 9, 12, 0, 0)
    assert _relative_time_uk(now - timedelta(hours=1, minutes=5), now) == "1 годину тому"
    assert _relative_time_uk(now - timedelta(hours=3), now) == "3 години тому"
    assert _relative_time_uk(now - timedelta(hours=11), now) == "11 годин тому"


# ---------------------------------------------------------------------------
# _sync_heartbeat_status — pure formatting/precedence logic.
# ---------------------------------------------------------------------------

INTERVAL = 120  # seconds, matches MAIL_SYNC_INTERVAL_SECONDS / SHEET_SYNC_INTERVAL_SECONDS


def test_unconfigured_wins_over_any_recorded_heartbeat():
    now = datetime(2026, 8, 9, 12, 0, 0)
    heartbeat = SyncHeartbeat(last_attempt_at=now, status="ok")

    result = _sync_heartbeat_status(
        heartbeat, configured=False, interval_seconds=INTERVAL, now=now
    )

    assert result == {"state": "neutral", "label": "не налаштовано"}


def test_configured_but_never_attempted_reads_as_neutral_unknown():
    now = datetime(2026, 8, 9, 12, 0, 0)
    heartbeat = SyncHeartbeat()  # fresh process, no tick has completed yet

    result = _sync_heartbeat_status(
        heartbeat, configured=True, interval_seconds=INTERVAL, now=now
    )

    assert result["state"] == "neutral"
    assert "перевірки" in result["label"]


def test_recent_success_reads_as_success_with_relative_time():
    now = datetime(2026, 8, 9, 12, 5, 0)
    heartbeat = SyncHeartbeat(last_attempt_at=now - timedelta(minutes=1), status="ok")

    result = _sync_heartbeat_status(
        heartbeat, configured=True, interval_seconds=INTERVAL, now=now
    )

    assert result["state"] == "success"
    assert result["label"] == "✓ 1 хвилину тому"


def test_recent_failure_reads_as_error_with_relative_time():
    now = datetime(2026, 8, 9, 12, 5, 0)
    heartbeat = SyncHeartbeat(
        last_attempt_at=now - timedelta(minutes=2),
        status="error",
        error_message="Не вдалося синхронізувати пошту.",
    )

    result = _sync_heartbeat_status(
        heartbeat, configured=True, interval_seconds=INTERVAL, now=now
    )

    assert result["state"] == "error"
    assert result["label"] == "⚠ помилка · 2 хвилини тому"


def test_busy_skip_with_no_prior_outcome_reads_as_neutral_not_error():
    """First tick ever hits a lock held by a concurrent manual sync — this
    must not display as an error, since nothing actually failed."""
    now = datetime(2026, 8, 9, 12, 0, 30)
    heartbeat = SyncHeartbeat(last_attempt_at=now, status="unknown")

    result = _sync_heartbeat_status(
        heartbeat, configured=True, interval_seconds=INTERVAL, now=now
    )

    assert result["state"] == "neutral"
    assert result["state"] != "error"


def test_stale_last_attempt_overrides_previously_recorded_success():
    """The scariest failure mode: the worker thread itself died. A last
    recorded "ok" from long ago must not keep showing as healthy forever."""
    now = datetime(2026, 8, 9, 13, 0, 0)
    stale_at = now - timedelta(seconds=INTERVAL * 3 + 1)
    heartbeat = SyncHeartbeat(last_attempt_at=stale_at, status="ok")

    result = _sync_heartbeat_status(
        heartbeat, configured=True, interval_seconds=INTERVAL, now=now
    )

    assert result["state"] == "warning"


def test_stale_last_attempt_overrides_previously_recorded_error_too():
    now = datetime(2026, 8, 9, 13, 0, 0)
    stale_at = now - timedelta(seconds=INTERVAL * 3 + 1)
    heartbeat = SyncHeartbeat(last_attempt_at=stale_at, status="error", error_message="x")

    result = _sync_heartbeat_status(
        heartbeat, configured=True, interval_seconds=INTERVAL, now=now
    )

    assert result["state"] == "warning"


def test_just_under_stale_threshold_still_reads_as_last_recorded_outcome():
    now = datetime(2026, 8, 9, 13, 0, 0)
    almost_stale = now - timedelta(seconds=INTERVAL * 3 - 1)
    heartbeat = SyncHeartbeat(last_attempt_at=almost_stale, status="ok")

    result = _sync_heartbeat_status(
        heartbeat, configured=True, interval_seconds=INTERVAL, now=now
    )

    assert result["state"] == "success"


# ---------------------------------------------------------------------------
# _record_sync_heartbeat — isolated from the real module-level dict so tests
# never leak state into each other or into a real running instance.
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_heartbeats(monkeypatch):
    fresh = {"mail": SyncHeartbeat(), "sheet": SyncHeartbeat()}
    monkeypatch.setattr(web, "_sync_heartbeats", fresh)
    return fresh


def test_record_success_sets_ok_and_clears_error(isolated_heartbeats):
    web._record_sync_heartbeat("mail", status="ok")

    heartbeat = web._sync_heartbeats["mail"]
    assert heartbeat.status == "ok"
    assert heartbeat.error_message is None
    assert heartbeat.last_attempt_at is not None


def test_record_error_stores_safe_message(isolated_heartbeats):
    web._record_sync_heartbeat("sheet", status="error", error_message="Не вдалося синхронізувати Google Таблицю.")

    heartbeat = web._sync_heartbeats["sheet"]
    assert heartbeat.status == "error"
    assert heartbeat.error_message == "Не вдалося синхронізувати Google Таблицю."


def test_record_skip_advances_timestamp_but_keeps_prior_status(isolated_heartbeats):
    web._record_sync_heartbeat("mail", status="error", error_message="кабельний збій")
    first_attempt = web._sync_heartbeats["mail"].last_attempt_at

    web._record_sync_heartbeat("mail", status="skipped")

    heartbeat = web._sync_heartbeats["mail"]
    assert heartbeat.status == "error"  # sticky — busy is not a new outcome
    assert heartbeat.error_message == "кабельний збій"
    assert heartbeat.last_attempt_at >= first_attempt  # loop is still alive


# ---------------------------------------------------------------------------
# _mail_sync_tick / _sheet_sync_tick — the worker-loop dispatch, without
# threads/Event.
# ---------------------------------------------------------------------------


def test_mail_tick_skips_and_leaves_heartbeat_untouched_when_unconfigured(
    isolated_heartbeats, monkeypatch
):
    monkeypatch.setattr(web, "_imap_configured", lambda db: False)
    monkeypatch.setattr(
        web,
        "sync_mail_background",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not sync when unconfigured")),
    )

    web._mail_sync_tick(db=None)

    assert web._sync_heartbeats["mail"] == SyncHeartbeat()


def test_mail_tick_records_success(isolated_heartbeats, monkeypatch):
    monkeypatch.setattr(web, "_imap_configured", lambda db: True)
    monkeypatch.setattr(web, "sync_mail_background", lambda db, path: 3)
    monkeypatch.setattr(web, "_auto_accept_pass", lambda db: 0)

    web._mail_sync_tick(db=None)

    heartbeat = web._sync_heartbeats["mail"]
    assert heartbeat.status == "ok"
    assert heartbeat.error_message is None


def test_mail_tick_records_skip_on_busy_without_marking_error(isolated_heartbeats, monkeypatch):
    monkeypatch.setattr(web, "_imap_configured", lambda db: True)

    def raise_busy(db, path):
        raise MailSyncBusyError("Синхронізація пошти вже виконується.")

    monkeypatch.setattr(web, "sync_mail_background", raise_busy)

    web._mail_sync_tick(db=None)

    heartbeat = web._sync_heartbeats["mail"]
    assert heartbeat.status != "error"
    assert heartbeat.status == "skipped" or heartbeat.status == "unknown"
    assert heartbeat.last_attempt_at is not None


def test_mail_tick_records_safe_error_message(isolated_heartbeats, monkeypatch):
    monkeypatch.setattr(web, "_imap_configured", lambda db: True)

    def raise_sync_error(db, path):
        raise MailSyncError("Не вдалося синхронізувати пошту. Перевірте IMAP-логін.")

    monkeypatch.setattr(web, "sync_mail_background", raise_sync_error)

    web._mail_sync_tick(db=None)

    heartbeat = web._sync_heartbeats["mail"]
    assert heartbeat.status == "error"
    assert heartbeat.error_message == "Не вдалося синхронізувати пошту. Перевірте IMAP-логін."


def test_mail_tick_lets_unexpected_exceptions_propagate_to_worker_loop(
    isolated_heartbeats, monkeypatch
):
    """Genuinely unexpected exceptions are the outer _mail_sync_worker loop's
    job (its own generic except-all records the failure heartbeat) — the
    tick function itself must not swallow anything it doesn't recognize."""
    monkeypatch.setattr(web, "_imap_configured", lambda db: True)

    def raise_unexpected(db, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "sync_mail_background", raise_unexpected)

    with pytest.raises(RuntimeError):
        web._mail_sync_tick(db=None)


def test_sheet_tick_records_success(isolated_heartbeats, monkeypatch):
    monkeypatch.setattr(web, "_sheets_configured", lambda db: True)
    monkeypatch.setattr(web, "sync_sheets_background", lambda db: None)

    web._sheet_sync_tick(db=None)

    assert web._sync_heartbeats["sheet"].status == "ok"


def test_sheet_tick_records_safe_error_message(isolated_heartbeats, monkeypatch):
    monkeypatch.setattr(web, "_sheets_configured", lambda db: True)

    def raise_sync_error(db):
        raise SheetSyncError("Не вдалося синхронізувати Google Таблицю.")

    monkeypatch.setattr(web, "sync_sheets_background", raise_sync_error)

    web._sheet_sync_tick(db=None)

    heartbeat = web._sync_heartbeats["sheet"]
    assert heartbeat.status == "error"
    assert heartbeat.error_message == "Не вдалося синхронізувати Google Таблицю."


def test_sheet_tick_records_skip_on_busy_without_marking_error(isolated_heartbeats, monkeypatch):
    monkeypatch.setattr(web, "_sheets_configured", lambda db: True)

    def raise_busy(db):
        raise SheetSyncBusyError("Синхронізація Google Таблиці вже виконується.")

    monkeypatch.setattr(web, "sync_sheets_background", raise_busy)

    web._sheet_sync_tick(db=None)

    heartbeat = web._sync_heartbeats["sheet"]
    assert heartbeat.status != "error"
    assert heartbeat.last_attempt_at is not None


# ---------------------------------------------------------------------------
# _queue_sync_status — the small glue function get_queue calls, combining
# configuration state with the recorded heartbeat for both sync types.
# ---------------------------------------------------------------------------


def test_queue_sync_status_reports_not_configured_for_both_when_fresh(isolated_heartbeats, monkeypatch):
    monkeypatch.setattr(web, "_imap_configured", lambda db: False)
    monkeypatch.setattr(web, "_sheets_configured", lambda db: False)

    result = web._queue_sync_status(db=None, now=datetime(2026, 8, 9, 12, 0, 0))

    assert result["mail"]["label"] == "не налаштовано"
    assert result["sheet"]["label"] == "не налаштовано"
