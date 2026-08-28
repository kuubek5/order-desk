"""Пульс фонових синхронізацій — «чи цикл узагалі ще цокає».

Це сигнал живості, а не журнал. SyncLog лишається аудитом і свідомо НЕ пише
рядка на тихий фоновий тік, тож мовчання там неоднозначне: «здоровий і тихий»
чи «мертвий». Пульс оновлюється на КОЖНОМУ тіку саме щоб зняти цю
неоднозначність.

Живе тільки в памʼяті й не переживає рестарт — показати «очікує першої
перевірки» після перезапуску чесно, а не баг.

Пишуть сюди фонові воркери, читають екрани; тому модуль стоїть окремо від
обох (сусід app/sync_control.py).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.services.config_state import imap_configured, sheets_configured
from app.services.formatting import relative_time_uk
from app.sync_control import MAIL_SYNC_INTERVAL_SECONDS, SHEET_SYNC_INTERVAL_SECONDS

# A heartbeat last-attempt older than this many sync intervals means the
# background loop itself likely died (thread crashed, process wedged) rather
# than just "ran and found nothing" — the scariest failure mode, since it's
# the monitoring signal silently going quiet. See heartbeat_status.
STALE_HEARTBEAT_MULTIPLIER = 3


@dataclass(frozen=True)
class SyncHeartbeat:
    """Last-tick outcome of a background sync loop (mail IMAP or Google
    Sheets), kept in memory only — this is a liveness signal ("is the loop
    ticking right now"), not an audit trail. SyncLog remains the audit
    trail and deliberately writes no row for a quiet/no-op background tick
    or a background-triggered failure (see mail_sync_service.sync_mail /
    sheet_sync_service.sync_google_sheets's `persist=trigger == "manual"`),
    so silence there is ambiguous between "healthy and quiet" and "dead".
    This heartbeat is updated on every single tick regardless, to remove
    that ambiguity. Not persisted to the DB and does not survive a
    restart — showing "unknown" until the next tick completes after a
    restart is correct and honest, not a bug.
    """

    last_attempt_at: datetime | None = None
    status: str = "unknown"  # "unknown" | "ok" | "error" | "skipped"
    error_message: str | None = None


# Keyed by sync type. Only the matching background worker thread ever writes
# its own key (mail worker writes "mail", sheet worker writes "sheet"), so
# there is never more than one writer per key and a Lock isn't needed for
# the write side. Request-handling threads only read this dict to render the
# queue page. Each write below swaps in a brand-new *immutable* SyncHeartbeat
# instance in one dict-key assignment — under the GIL that single assignment
# is atomic, so a concurrent reader always sees either the old or the new
# heartbeat in full, never a partially-updated one. Do not turn this into a
# multi-step mutation (e.g. `heartbeat.status = ...`) — that would reopen the
# torn-read risk this comment is explaining away.
heartbeats: dict[str, SyncHeartbeat] = {
    "mail": SyncHeartbeat(),
    "sheet": SyncHeartbeat(),
}


def record_heartbeat(key: str, *, status: str, error_message: str | None = None) -> None:
    """Record one background sync tick's outcome for the queue page's status pair.

    ``status="skipped"`` (another sync already holds the lock — MailSyncBusyError /
    SheetSyncBusyError) is deliberately neutral: it proves the loop is alive
    (last_attempt_at advances, which is what staleness detection cares about)
    without overwriting a previously recorded real outcome with a false error.
    """
    now = datetime.now()
    if status == "skipped":
        previous = heartbeats[key]
        heartbeats[key] = SyncHeartbeat(
            last_attempt_at=now, status=previous.status, error_message=previous.error_message
        )
    else:
        heartbeats[key] = SyncHeartbeat(
            last_attempt_at=now, status=status, error_message=error_message
        )


def heartbeat_status(
    heartbeat: SyncHeartbeat,
    *,
    configured: bool,
    interval_seconds: int,
    now: datetime,
) -> dict[str, str]:
    """Pure formatting for one sync-status line in the queue sidebar.

    Precedence: unconfigured beats everything (nothing is supposed to be
    running, so silence isn't a warning sign) — then staleness (see
    STALE_HEARTBEAT_MULTIPLIER) beats whatever outcome was last recorded,
    because a dead worker thread is worse than a recorded failure — only
    then do we fall back to the last real success/error tick.
    """
    if not configured:
        return {"state": "neutral", "label": "не налаштовано"}
    if heartbeat.last_attempt_at is None:
        return {"state": "neutral", "label": "очікує першої перевірки"}

    age_seconds = max(0.0, (now - heartbeat.last_attempt_at).total_seconds())
    if age_seconds > interval_seconds * STALE_HEARTBEAT_MULTIPLIER:
        return {"state": "warning", "label": "⚠ немає відповіді від фонового процесу"}

    relative = relative_time_uk(heartbeat.last_attempt_at, now)
    if heartbeat.status == "error":
        return {"state": "error", "label": f"⚠ помилка · {relative}"}
    if heartbeat.status == "ok":
        return {"state": "success", "label": f"✓ {relative}"}
    # "skipped" with no prior real outcome yet (busy on the very first tick
    # this process ever attempted) — rare, but still an honest "unknown".
    return {"state": "neutral", "label": "очікує результату"}


def sync_status_pair(db: Session, now: datetime) -> dict[str, dict[str, str]]:
    """Пара індикаторів «пошта / таблиця» для бічної панелі черги."""
    return {
        "mail": heartbeat_status(
            heartbeats["mail"],
            configured=imap_configured(db),
            interval_seconds=MAIL_SYNC_INTERVAL_SECONDS,
            now=now,
        ),
        "sheet": heartbeat_status(
            heartbeats["sheet"],
            configured=sheets_configured(db),
            interval_seconds=SHEET_SYNC_INTERVAL_SECONDS,
            now=now,
        ),
    }
