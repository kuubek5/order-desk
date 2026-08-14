"""Automatic monthly database snapshots.

At the turn of each month the whole working database (orders, clients,
history, settings — everything) is archived as a standalone SQLite file named
after the month it closes, e.g. ``orderdesk-2026-07.db`` for July 2026, under
``<db dir>/backups/monthly/``.

Timing is deliberately NOT "at midnight on the 1st": the lab PC is routinely
off at that moment. Instead the app checks on startup and every few hours
(see ``_monthly_backup_worker`` in app/web.py) whether the *previous* month's
snapshot exists yet, and creates it at the first opportunity after the month
rolled over. The snapshot is a point-in-time copy taken then — for a
lab that works day-to-day this is exactly "база на кінець місяця".

The copy itself uses SQLite's ``VACUUM INTO``: it produces a consistent,
compacted, self-contained database file even while the app keeps reading and
writing (WAL), with no downtime and no table-by-table export logic to drift
out of date as the schema grows. Written to a ``.tmp`` first and renamed into
place, so a crash mid-copy never leaves a half-written file that would make
the "already exists" check lie forever after.

Note the DPAPI caveat shared with the raw DB file: secrets inside the
snapshot stay encrypted under THIS machine's key, so a snapshot restored on
another PC yields all operational data but undecryptable credentials — for
machine moves use the password-protected export in app/backup.py instead
(Налаштування → Резервна копія).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "orderdesk-"
# Safety valve, not a policy: two decades of monthly files. Snapshots are a
# few MB each, so keeping them all is the point — this only guards against a
# pathological loop ever flooding the folder.
MAX_SNAPSHOTS = 240

MONTH_NAMES_UK = {
    1: "січень", 2: "лютий", 3: "березень", 4: "квітень",
    5: "травень", 6: "червень", 7: "липень", 8: "серпень",
    9: "вересень", 10: "жовтень", 11: "листопад", 12: "грудень",
}


def previous_month(today: date) -> tuple[int, int]:
    """(year, month) of the month before `today`'s."""
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def snapshot_filename(year: int, month: int) -> str:
    return f"{SNAPSHOT_PREFIX}{year:04d}-{month:02d}.db"


def month_label_uk(year: int, month: int) -> str:
    """Readable Ukrainian label for the UI, e.g. "липень 2026"."""
    return f"{MONTH_NAMES_UK.get(month, str(month))} {year}"


def backups_dir(db_path: str | Path) -> Path:
    """Monthly snapshots live next to the working DB (same disk, same
    permissions, included in any folder-level copy the admin already does)."""
    return Path(db_path).expanduser().resolve().parent / "backups" / "monthly"


def list_snapshots(db_path: str | Path) -> list[Path]:
    """Existing snapshot files, newest month first (name-sorted — the
    zero-padded ISO name makes lexical order chronological)."""
    folder = backups_dir(db_path)
    try:
        files = [
            p for p in folder.iterdir()
            if p.is_file() and p.name.startswith(SNAPSHOT_PREFIX) and p.suffix == ".db"
        ]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name, reverse=True)


def ensure_monthly_snapshot(
    engine: Engine, db_path: str | Path, today: date | None = None
) -> Path | None:
    """Create the previous month's snapshot if it doesn't exist yet.

    Returns the created file's path, or None when the snapshot already exists
    (the normal case for all but one tick per month). Raises on copy failure —
    the caller logs and retries on its next tick."""
    today = today or date.today()
    year, month = previous_month(today)
    folder = backups_dir(db_path)
    target = folder / snapshot_filename(year, month)
    if target.exists():
        return None

    folder.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".db.tmp")
    # A leftover .tmp from a crashed attempt must not fail VACUUM INTO
    # (it refuses to overwrite an existing file).
    tmp.unlink(missing_ok=True)

    # VACUUM INTO cannot run inside a transaction; exec on a raw autocommit
    # connection. The path is server-controlled (no user input); the quote
    # doubling below is belt-and-braces for exotic install paths.
    quoted = str(tmp).replace("'", "''")
    with engine.connect() as conn:
        conn.exec_driver_sql(f"VACUUM INTO '{quoted}'")

    tmp.replace(target)
    logger.info("Monthly DB snapshot created: %s", target.name)

    _prune(folder)
    return target


def _prune(folder: Path) -> None:
    snapshots = sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.name.startswith(SNAPSHOT_PREFIX) and p.suffix == ".db"),
        key=lambda p: p.name,
    )
    excess = len(snapshots) - MAX_SNAPSHOTS
    for path in snapshots[:max(0, excess)]:
        try:
            path.unlink()
            logger.info("Pruned old monthly snapshot: %s", path.name)
        except OSError:
            logger.warning("Could not prune old snapshot %s", path.name)
