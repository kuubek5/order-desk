"""Mail-spool disk usage and its cleanup — daily by itself and by a button.

`mail_attachments/<uid_validity>_<uid>/` accumulates one folder per imported
letter (теки, створені до складеного імені, звуться самим uid і читаються
далі — див. `folder_candidates`). Accepted letters have their files MOVED into
export, but letters that were handled by hand (operators download from ukr.net
themselves and clean the mailbox directly) keep theirs forever.

Правило власника 29.09.26 («поставити правило видаляти їх після 2 днів»), одне
для щоденного прибирання (`web._shift_images_prune_tick`) і кнопки в
Налаштуваннях. Тека прибирається, коли ЛИСТ ЗАВЕРШЕНО щонайменше
`DEFAULT_PRUNE_AFTER_DAYS` днів тому:

  * порожня тека — завжди (прийнятий лист: файли вже в export);
  * тека без листа в базі — завжди, ЯКЩО в ній не лежать файли
    незавершеного листа (`Attachment.saved_path`): ім'я теки — з UID, але
    після «Повернути в тріаж» файли лишаються в теці за старим UID (05.10.26);
  * прийнятий лист — від моменту, коли покинув Вхідні (інакше від приходу);
  * відхилений — від приходу листа;
  * неприйнятий лист, що ПОКИНУВ Вхідні (переклали в папку чи видалили в
    пошті, `mailbox_folder` / `inbox_gone_at`) — від моменту, коли покинув.

НІКОЛИ не чіпаємо лист, що досі у «Вхідних», і лист «На уточненні» (`hold_at`),
скільки б він не лежав: його ще приймати, а без файлів прийняти не можна. Не
чіпаємо і те, чого не довести (неприйнятий лист без жодного часу).

Файли в export не чіпаємо ніколи. Прибране не безповоротно: лист лежить у
пошті, повернутий у Вхідні — скачується наново.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from time import monotonic
import logging
from pathlib import Path, PureWindowsPath
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Attachment, EmailMessage

logger = logging.getLogger(__name__)

DEFAULT_PRUNE_AFTER_DAYS = 2


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


def _finished_at(
    status: str,
    received_at: datetime | None,
    mailbox_folder: str | None,
    mailbox_moved_at: datetime | None,
    inbox_gone_at: datetime | None,
    hold_at: datetime | None,
) -> datetime | None:
    """Коли лист ЗАВЕРШЕНО, тобто його файли у спулі вже не знадобляться.
    None — лист ще в роботі (або цього не довести): теку не чіпати."""
    if hold_at is not None:
        return None  # «На уточненні» — чекає відповіді клієнта
    left_inbox = mailbox_moved_at or inbox_gone_at
    if status == "відхилено":
        return received_at or datetime.min
    if status == "прийнято":
        return left_inbox or received_at or datetime.min
    if status == "нове":
        if mailbox_folder is None and inbox_gone_at is None:
            return None  # досі у Вхідних — його ще приймати
        # Перенесені до появи `mailbox_moved_at` — від приходу листа.
        return left_inbox or received_at
    return None


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
    letters: dict[str, list[datetime | None]] = {}
    for row in session.execute(
        select(
            EmailMessage.uid, EmailMessage.uid_validity,
            EmailMessage.status, EmailMessage.received_at,
            EmailMessage.mailbox_folder, EmailMessage.mailbox_moved_at,
            EmailMessage.inbox_gone_at, EmailMessage.hold_at,
        )
    ).all():
        done = _finished_at(
            row.status, row.received_at, row.mailbox_folder,
            row.mailbox_moved_at, row.inbox_gone_at, row.hold_at,
        )
        for name in folder_candidates(row.uid, row.uid_validity):
            letters.setdefault(name, []).append(done)

    # Теки, у яких лежать файли НЕЗАВЕРШЕНОГО листа, — за `saved_path`, а не за
    # іменем. Ім'я теки береться з UID, але файли бувають і в теці під іншим
    # ім'ям: «Повернути в тріаж» кладе їх у теку за старим UID, а лист у пошті
    # отримує новий. Прибиральник, що дивився лише на ім'я, стирав файли
    # листа, який ще у «Вхідних» (MULTICALC_SAFETY_BRIEF.md п.4, 05.10.26).
    # Порівнюємо імена складових шляху, а не повний шлях: `saved_path` буває
    # UNC, а корінь спулу — з літерою диска (§14 «Шляхи»). Зайвий збіг імені
    # лише вбереже теку, а не зітре чужу.
    live_parts: set[str] = set()
    for row in session.execute(
        select(
            Attachment.saved_path,
            EmailMessage.status, EmailMessage.received_at,
            EmailMessage.mailbox_folder, EmailMessage.mailbox_moved_at,
            EmailMessage.inbox_gone_at, EmailMessage.hold_at,
        ).join(EmailMessage, Attachment.email_message_id == EmailMessage.id)
    ).all():
        if not row.saved_path:
            continue
        done = _finished_at(
            row.status, row.received_at, row.mailbox_folder,
            row.mailbox_moved_at, row.inbox_gone_at, row.hold_at,
        )
        if done is None or done >= cutoff:
            live_parts.update(part.casefold() for part in PureWindowsPath(row.saved_path).parts[:-1])

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

        if child.name.casefold() in live_parts:
            continue  # файли незавершеного листа — див. `live_parts`
        entries = letters.get(child.name)
        if not entries:
            # No letter row owns this folder any more.
            prunable.append(child)
            prunable_bytes += size
            continue
        if size == 0:
            prunable.append(child)
            continue
        if all(done is not None and done < cutoff for done in entries):
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
