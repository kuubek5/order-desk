"""Mail-spool disk usage and a conservative, operator-triggered cleanup.

`mail_attachments/<uid_validity>_<uid>/` accumulates one folder per imported
letter (теки, створені до складеного імені, звуться самим uid і читаються
далі — див. `folder_candidates`). Accepted
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
from threading import Lock
from time import monotonic
import logging
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage

logger = logging.getLogger(__name__)

DEFAULT_PRUNE_AFTER_DAYS = 30


def spool_folder_name(uid: str, uid_validity: str | None) -> str:
    """Імʼя теки спулу для листа: `<uid_validity>_<uid>`.

    IMAP UID унікальний ЛИШЕ в межах поточного UIDVALIDITY (див. міграцію
    0045, яка з тієї ж причини зробила унікальним складений ключ у базі).
    Тека ж називалась самим uid — тож після перестворення скриньки два різні
    листи діставали одну теку, і вкладення одного лягали поруч із вкладеннями
    іншого. На видачі це «двійник, і невідомо, який справжній».

    Порожній uid_validity (рядки, створені до 0045, і скриньки, які його не
    віддають) лишає старе імʼя — теки з файлами не перейменовуються, бо на них
    посилаються збережені шляхи вкладень; сумісність тут дешевша за міграцію
    диска (див. `folder_candidates`).
    """
    validity = (uid_validity or "").strip()
    return f"{validity}_{uid}" if validity else str(uid)


def folder_candidates(uid: str, uid_validity: str | None) -> tuple[str, ...]:
    """Усі імена теки, які можуть належати цьому листу: нове й старе.

    Потрібно скрізь, де тека шукається за листом, а не навпаки: у листа, який
    приїхав до переходу на складене імʼя, файли лежать у теці зі старою
    назвою, і вважати її «нічиєю» не можна — прибиральник спулу видалив би
    живі вкладення.
    """
    new = spool_folder_name(uid, uid_validity)
    legacy = str(uid)
    return (new,) if new == legacy else (new, legacy)


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


# Кеш звіту про спул. `analyze_spool` рахує РОЗМІР кожної теки — тобто
# обходить весь спул із stat() на кожен файл, по мережевій шарі. Це робилось
# на КОЖНОМУ відкритті /settings, хоча цифра змінюється хіба після приймання
# листа чи прибирання (ревʼю 07.09.26, M.5). Тепер результат живе кілька
# хвилин; кнопка «Прибрати» скидає його явно, щоб адмін одразу бачив ефект.
_REPORT_TTL_SECONDS = 300.0
_report_lock = Lock()
_report_cache: dict[str, tuple[float, "SpoolReport"]] = {}


def clear_spool_report_cache() -> None:
    with _report_lock:
        _report_cache.clear()


def analyze_spool_cached(
    session: Session, spool_root: Path, *, older_than_days: int = DEFAULT_PRUNE_AFTER_DAYS
) -> "SpoolReport":
    """`analyze_spool` із коротким кешем — для екранів, які просто показують
    цифру. Рішення про видалення приймається на свіжому звіті (`prune_spool`
    рахує сам), тож застаріла на кілька хвилин цифра нічим не ризикує."""
    key = f"{spool_root}|{older_than_days}"
    now = monotonic()
    with _report_lock:
        cached = _report_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

    report = analyze_spool(session, spool_root, older_than_days=older_than_days)
    with _report_lock:
        _report_cache[key] = (now + _REPORT_TTL_SECONDS, report)
    return report


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
    # ІМʼЯ ТЕКИ -> [(status, received_at), ...]. Нові теки звуться
    # `<uid_validity>_<uid>`, старі — самим uid, і лист може володіти текою в
    # будь-якому з двох форматів (`folder_candidates`), тож реєструємо обидва.
    #
    # Агрегуємо СПИСКОМ, а не «останній виграє»: після зміни UIDVALIDITY два
    # рядки можуть ділити uid, і застарілий «відхилено» затінював би живий
    # «нове», позначаючи потрібну теку прибираною. Тека прибирається за
    # статусом лише коли ВСІ рядки, що на неї претендують, згодні.
    letters: dict[str, list[tuple[str, datetime | None]]] = {}
    for uid, uid_validity, status, received_at in session.execute(
        select(
            EmailMessage.uid, EmailMessage.uid_validity,
            EmailMessage.status, EmailMessage.received_at,
        )
    ).all():
        for name in folder_candidates(uid, uid_validity):
            letters.setdefault(name, []).append((status, received_at))

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

        entries = letters.get(child.name)
        if not entries:
            # No letter row owns this folder any more.
            prunable.append(child)
            prunable_bytes += size
            continue
        if size == 0:
            prunable.append(child)
            continue
        if all(
            status == "відхилено" and (received_at is None or received_at < cutoff)
            for status, received_at in entries
        ):
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
    # Цифра на екрані мусить одразу показати ефект кнопки, а не висіти
    # застарілою до кінця TTL.
    clear_spool_report_cache()
    return removed, freed
