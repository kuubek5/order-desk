"""Mail-spool disk usage and a conservative, operator-triggered cleanup.

`mail_attachments/<uid>/` accumulates one folder per imported letter. Accepted
letters have their files MOVED into export (the spool folder is left empty),
but rejected letters — and letters whose files nobody ever needed — keep theirs
forever. With the «скачувати все» toggle on, that grows a lot faster.

Nothing here runs automatically. Deleting an operator's files is not a
background job's decision: the settings screen shows what could be freed and
the admin presses the button. The rules below are deliberately narrow:

  * empty folders — always safe,
  * folders of REJECTED letters older than `older_than_days`,
  * orphan folders whose letter no longer exists in the DB at all.

Pending/accepted/filtered letters are never touched, and neither is any file
still referenced by an Attachment row of a non-rejected letter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage

logger = logging.getLogger(__name__)

DEFAULT_PRUNE_AFTER_DAYS = 30


@dataclass(frozen=True)
class SpoolReport:
    total_bytes: int
    total_dirs: int
    prunable_bytes: int
    prunable_dirs: list[Path]

    @property
    def total_mb(self) -> float:
        return round(self.total_bytes / (1024 * 1024), 1)

    @property
    def prunable_mb(self) -> float:
        return round(self.prunable_bytes / (1024 * 1024), 1)


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def analyze_spool(
    session: Session,
    spool_root: Path,
    *,
    older_than_days: int = DEFAULT_PRUNE_AFTER_DAYS,
    now: datetime | None = None,
) -> SpoolReport:
    """Measure the spool and decide which folders a cleanup MAY remove.
    Read-only: touches no files."""
    root = Path(spool_root)
    if not root.is_dir():
        return SpoolReport(0, 0, 0, [])

    cutoff = (now or datetime.now()) - timedelta(days=older_than_days)
    # uid -> (status, received_at); the spool folder name IS the letter's uid.
    letters = {
        uid: (status, received_at)
        for uid, status, received_at in session.execute(
            select(EmailMessage.uid, EmailMessage.status, EmailMessage.received_at)
        ).all()
    }

    total_bytes = 0
    total_dirs = 0
    prunable_bytes = 0
    prunable: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        total_dirs += 1
        size = _dir_size(child)
        total_bytes += size

        entry = letters.get(child.name)
        if entry is None:
            # No letter row owns this folder any more.
            prunable.append(child)
            prunable_bytes += size
            continue
        status, received_at = entry
        if size == 0:
            prunable.append(child)
            continue
        if status == "відхилено" and (received_at is None or received_at < cutoff):
            prunable.append(child)
            prunable_bytes += size

    return SpoolReport(total_bytes, total_dirs, prunable_bytes, prunable)


def prune_spool(
    session: Session,
    spool_root: Path,
    *,
    older_than_days: int = DEFAULT_PRUNE_AFTER_DAYS,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Delete the folders analyze_spool marked prunable. Returns
    (folders_removed, bytes_freed). Recomputes the list itself rather than
    trusting a stale one from a previous page render."""
    report = analyze_spool(session, spool_root, older_than_days=older_than_days, now=now)
    removed = 0
    freed = 0
    for path in report.prunable_dirs:
        size = _dir_size(path)
        try:
            shutil.rmtree(path)
        except OSError:
            logger.warning("Could not remove spool folder %s", path)
            continue
        removed += 1
        freed += size
    if removed:
        logger.info("Mail spool cleanup removed %s folder(s), freed %s bytes", removed, freed)
    return removed, freed
