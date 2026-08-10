"""Transactional, user-safe orchestration for manual and background IMAP sync."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from sqlalchemy.orm import Session

from app.mail_reader import fetch_new_emails
from app.models import SyncLog


class MailSyncError(RuntimeError):
    """An error message that is safe to show to an operator."""


class MailSyncBusyError(MailSyncError):
    """Raised when another manual/background synchronization owns the lock."""


_sync_lock = Lock()


def _safe_error(exc: Exception) -> MailSyncError:
    if isinstance(exc, MailSyncError):
        return exc
    if isinstance(exc, RuntimeError) and str(exc).startswith("IMAP не налаштовано"):
        return MailSyncError(str(exc))
    return MailSyncError(
        "Не вдалося синхронізувати пошту. Перевірте IMAP-логін, пароль для "
        "програм та підключення до інтернету."
    )


def _cleanup_created_attachments(session: Session) -> None:
    """Remove only files written by the failed synchronization attempt."""
    paths = [Path(path) for path in session.info.pop("mail_sync_created_paths", [])]
    for path in reversed(paths):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
    for directory in sorted({path.parent for path in paths}, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _record_failure(session: Session, error: MailSyncError, *, persist: bool) -> None:
    session.rollback()
    _cleanup_created_attachments(session)
    if not persist:
        return
    session.add(
        SyncLog(
            direction="mail_to_db",
            status="error",
            # Never persist raw IMAP exceptions: they can contain server or
            # authentication details unsuitable for the UI and logs.
            message=str(error),
        )
    )
    try:
        session.commit()
    except Exception:
        session.rollback()


def sync_mail(session: Session, attachments_dir: Path, *, trigger: str = "manual") -> int:
    """Fetch recent IMAP messages and write an audit SyncLog.

    ``fetch_new_emails`` itself now commits progressively (a headers-only row
    per new message, then one more commit per message once its attachments are
    downloaded) so the triage screen shows new mail within seconds rather than
    after the whole batch finishes. The ``session.commit()`` below is a final
    safety commit for the audit SyncLog row — harmless no-op if there's
    nothing left pending.

    The process-wide non-blocking lock prevents a background run and a button
    click from importing the same message concurrently. ``trigger`` is audit
    metadata only and accepts ``manual`` or ``background``.
    """
    if trigger not in {"manual", "background"}:
        raise ValueError("unsupported mail sync trigger")
    if not _sync_lock.acquire(blocking=False):
        raise MailSyncBusyError(
            "Синхронізація пошти вже виконується. Спробуйте трохи пізніше."
        )

    try:
        try:
            session.info["mail_sync_created_paths"] = []
            created = fetch_new_emails(session, attachments_dir)
            if trigger == "manual" or created:
                session.add(
                    SyncLog(
                        direction="mail_to_db",
                        status="ok",
                        message=f"trigger {trigger}; created {created}",
                    )
                )
            session.commit()
            session.info.pop("mail_sync_created_paths", None)
            return created
        except Exception as exc:
            safe_error = _safe_error(exc)
            # Manual failures remain visible in the audit table. Background
            # failures go to the rotating application log, avoiding a DB write
            # every two minutes during an internet outage.
            _record_failure(session, safe_error, persist=trigger == "manual")
            raise safe_error from exc
    finally:
        _sync_lock.release()


def sync_mail_background(session: Session, attachments_dir: Path) -> int:
    """Background-job entry point sharing the same locking and audit path."""
    return sync_mail(session, attachments_dir, trigger="background")


def sync_mailbox(session: Session, attachments_dir: Path, *, trigger: str = "manual") -> int:
    """Stable web/background API; returns the number of newly imported messages."""
    return sync_mail(session, attachments_dir, trigger=trigger)
