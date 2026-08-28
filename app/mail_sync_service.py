"""Transactional, user-safe orchestration for manual and background IMAP sync."""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock, Thread

from sqlalchemy.orm import Session

from app.mail_reader import fetch_new_emails
from app.models import SyncLog

logger = logging.getLogger(__name__)


class MailSyncError(RuntimeError):
    """An error message that is safe to show to an operator."""


class MailSyncBusyError(MailSyncError):
    """Raised when another manual/background synchronization owns the lock."""


class MailSyncTimeoutError(MailSyncError):
    """The IMAP fetch exceeded MAIL_SYNC_DEADLINE_SECONDS and was abandoned."""


_sync_lock = Lock()

# Watchdog deadline for one whole sync run. imap_tools' per-operation socket
# timeout (IMAP_TIMEOUT_SECONDS=20) does NOT cover a half-open socket or a
# server that keeps trickling bytes — either can hang a fetch indefinitely.
# Without this, a hung fetch held _sync_lock forever: the background loop
# stalled silently, the heartbeat went stale («⚠ немає відповіді»), and the
# manual «Перевірити пошту» button answered «вже виконується» until the app
# was restarted. A normal run (30-day lookback, tens of letters, the lab's
# slow TLS proxy) finishes well inside this; it's a last line of defence,
# not a tuning knob.
MAIL_SYNC_DEADLINE_SECONDS = 180


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


def _fetch_with_deadline(session: Session, attachments_dir: Path) -> int:
    """Run fetch_new_emails in a worker thread and give up after
    MAIL_SYNC_DEADLINE_SECONDS.

    A Python thread can't be killed, so on timeout the worker is simply
    abandoned (daemon — it can't block shutdown) and MailSyncTimeoutError is
    raised so the caller releases _sync_lock and the next tick starts clean.
    IMPORTANT for the caller: after a timeout the `session` object is still
    owned by the zombie worker — it must NOT be touched (no rollback, no
    commit) from this thread; SQLAlchemy sessions are not thread-safe.
    The zombie's eventual outcome is irrelevant: fetch_new_emails commits per
    letter with UID dedupe, so whatever it manages to land is consistent, and
    anything it doesn't is re-fetched by a later run.
    """
    result: dict[str, object] = {}

    def _run() -> None:
        try:
            result["created"] = fetch_new_emails(session, attachments_dir)
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller
            result["error"] = exc

    worker = Thread(target=_run, name="mail-sync-fetch", daemon=True)
    worker.start()
    worker.join(MAIL_SYNC_DEADLINE_SECONDS)
    if worker.is_alive():
        logger.error(
            "Mail sync exceeded %ss deadline — abandoning hung IMAP fetch",
            MAIL_SYNC_DEADLINE_SECONDS,
        )
        raise MailSyncTimeoutError(
            "Пошта не відповіла вчасно (зависло зʼєднання з IMAP). "
            "Наступна перевірка запуститься автоматично."
        )
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return int(result.get("created", 0))  # type: ignore[arg-type]


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
            created = _fetch_with_deadline(session, attachments_dir)
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
        except MailSyncTimeoutError:
            # The hung worker still owns `session` — do NOT rollback/commit it
            # here (see _fetch_with_deadline). Just release the lock and
            # surface the error; the caller logs/heartbeats it.
            raise
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


def run_sync_owned_session(*, trigger: str) -> int:
    """Run one mail sync on a session this function owns and closes — EXCEPT
    after a watchdog timeout, when the hung fetch thread still holds that
    session and closing it from here would yank the connection out from under
    it (sessions aren't thread-safe). In that case the session is deliberately
    leaked to the zombie (daemon thread; a single SQLite connection) and the
    error propagates so the caller can log/heartbeat/toast it."""
    from app.config import MAIL_ATTACHMENTS_PATH
    from app.db import SessionLocal

    sync_db = SessionLocal()
    timed_out = False
    try:
        # Background goes through sync_mail_background (the module-level name
        # the heartbeat tests monkeypatch); manual through sync_mailbox.
        if trigger == "background":
            return sync_mail_background(sync_db, Path(MAIL_ATTACHMENTS_PATH))
        return sync_mailbox(sync_db, Path(MAIL_ATTACHMENTS_PATH), trigger=trigger)
    except MailSyncTimeoutError:
        timed_out = True
        raise
    finally:
        if not timed_out:
            sync_db.close()
