from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.mail_sync_service import MailSyncError, sync_mail, sync_mail_background
from app.models import SyncLog


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_sync_commits_success_and_audit_log(monkeypatch, tmp_path):
    monkeypatch.setattr("app.mail_sync_service.fetch_new_emails", lambda session, path: 3)
    with _session() as session:
        assert sync_mail(session, tmp_path) == 3
        log = session.scalar(select(SyncLog))
        assert (log.direction, log.status, log.message) == (
            "mail_to_db", "ok", "trigger manual; created 3"
        )


def test_background_sync_uses_same_audit_path(monkeypatch, tmp_path):
    monkeypatch.setattr("app.mail_sync_service.fetch_new_emails", lambda session, path: 1)
    with _session() as session:
        assert sync_mail_background(session, tmp_path) == 1
        assert session.scalar(select(SyncLog)).message == "trigger background; created 1"


def test_sync_hides_raw_imap_error_and_records_safe_failure(monkeypatch, tmp_path):
    def fail(session, path):
        raise OSError("server said password=top-secret")

    monkeypatch.setattr("app.mail_sync_service.fetch_new_emails", fail)
    with _session() as session:
        with pytest.raises(MailSyncError) as raised:
            sync_mail(session, tmp_path)
        assert "top-secret" not in str(raised.value)
        log = session.scalar(select(SyncLog))
        assert log.status == "error"
        assert "top-secret" not in (log.message or "")


def test_parallel_sync_is_rejected_without_calling_fetch(monkeypatch, tmp_path):
    import app.mail_sync_service as service

    def should_not_run(session, path):
        raise AssertionError("fetch must not run while another sync owns the lock")

    monkeypatch.setattr(service, "fetch_new_emails", should_not_run)
    assert service._sync_lock.acquire(blocking=False)
    try:
        with _session() as session, pytest.raises(MailSyncError, match="вже виконується"):
            sync_mail(session, Path(tmp_path))
    finally:
        service._sync_lock.release()


def test_failed_sync_removes_only_new_attachment_files(monkeypatch, tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_text("keep", encoding="utf-8")
    message_dir = tmp_path / "42"
    created = message_dir / "case.stl"

    def fail_after_write(session, path):
        message_dir.mkdir()
        created.write_bytes(b"partial")
        session.info.setdefault("mail_sync_created_paths", []).append(created)
        raise OSError("disk failure")

    monkeypatch.setattr("app.mail_sync_service.fetch_new_emails", fail_after_write)
    with _session() as session, pytest.raises(MailSyncError):
        sync_mail(session, tmp_path)

    assert existing.read_text(encoding="utf-8") == "keep"
    assert not created.exists()
    assert not message_dir.exists()


def test_background_failure_does_not_write_repeating_audit_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.mail_sync_service.fetch_new_emails",
        lambda session, path: (_ for _ in ()).throw(OSError("offline")),
    )
    with _session() as session:
        with pytest.raises(MailSyncError):
            sync_mail_background(session, tmp_path)
        assert session.scalar(select(SyncLog)) is None


def test_hung_fetch_hits_watchdog_and_releases_lock(monkeypatch, tmp_path):
    """A fetch that never returns must not hold _sync_lock forever: the
    watchdog abandons it after the deadline, raises a timeout error, and the
    very next sync can acquire the lock again (no «вже виконується»)."""
    import threading
    import app.mail_sync_service as service
    from app.mail_sync_service import MailSyncTimeoutError

    release = threading.Event()

    def hang(session, path):
        release.wait(10)  # simulates a half-open IMAP socket
        return 0

    monkeypatch.setattr(service, "fetch_new_emails", hang)
    monkeypatch.setattr(service, "MAIL_SYNC_DEADLINE_SECONDS", 0.3)

    with _session() as session:
        with pytest.raises(MailSyncTimeoutError, match="не відповіла вчасно"):
            sync_mail(session, tmp_path)
        # lock released → a follow-up sync is NOT rejected as busy
        assert service._sync_lock.acquire(blocking=False)
        service._sync_lock.release()
    release.set()  # let the zombie finish so the test process exits cleanly


def test_normal_fetch_inside_deadline_unaffected(monkeypatch, tmp_path):
    import app.mail_sync_service as service
    monkeypatch.setattr(service, "fetch_new_emails", lambda session, path: 2)
    monkeypatch.setattr(service, "MAIL_SYNC_DEADLINE_SECONDS", 5)
    with _session() as session:
        assert sync_mail(session, tmp_path) == 2


def test_fetch_exception_in_worker_is_reraised_as_safe_error(monkeypatch, tmp_path):
    """Exceptions inside the worker thread must surface to the caller (not
    vanish in the thread) and still go through the safe-error wrapping."""
    import app.mail_sync_service as service

    def boom(session, path):
        raise OSError("imap said secret=hunter2")

    monkeypatch.setattr(service, "fetch_new_emails", boom)
    with _session() as session:
        with pytest.raises(MailSyncError) as raised:
            sync_mail(session, tmp_path)
        assert "hunter2" not in str(raised.value)
