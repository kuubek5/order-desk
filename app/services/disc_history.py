"""Історія дисків із журналів Sum3D (`jobs_list.txt`) — ЛИШЕ читання.

Кожен прорахунок Sum3D дописує в журнал блок: дата, час, проєкт
(`2026-11-24_12-01-45.cam`), верстат, матеріал, товщина, ДИСК, усадка і перелік
деталей. Це готова історія «на який диск оператор поклав роботу», від 2021 року
й незалежно від CRM. Формат звірено з `Sum3dDll_x64.dll` (FUN_180319900).

Рядки `item NNN : TSnn - <stl>` несуть назви файлів, тобто імена пацієнтів. Вони
тут лише РАХУЮТЬСЯ: рядок не декодується, не зберігається й нікуди не віддається.

Файли самої Sum3D не змінюються й не блокуються: читаємо хвіст.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional

from sqlalchemy.orm import Session

from app import log_throttle

logger = logging.getLogger(__name__)

FILE_NAME = "jobs_list.txt"
LEARN_BYTES = 16 * 1024 * 1024
"""Хвіст журналу для навчання (роки)."""
LIVE_BYTES = 3 * 1024 * 1024
"""Хвіст журналу для картки: що з диском робили останні місяці."""
_TTL_SECONDS = 60.0

_PROJECT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})")


@dataclass(frozen=True)
class Job:
    at: datetime
    project: str
    """`2026-11-24_12-01-45` або порожньо — назва проєкту не наша."""
    machine: str
    blank: str
    """Назва диска без теки й розширення, малими літерами."""
    thickness: float
    shrink: float
    items: int

    @property
    def sum3d(self) -> str:
        return self.project[11:]


def _number(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        return 0.0


def stem_key(raw: str) -> str:
    """Назва диска з журналу чи з теки → однаковий ключ."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return name[:-4] if name.endswith(".blk") else name


_FIELDS = {b"date", b"time", b"project", b"machine", b"thickness", b"blank", b"shrink factor"}


def _job(block: dict[bytes, str], items: int) -> Optional[Job]:
    blank = stem_key(block.get(b"blank", ""))
    if not blank or b"date" not in block:
        return None
    try:
        at = datetime.strptime(f"{block[b'date']} {block.get(b'time', '')}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    found = _PROJECT_RE.search(block.get(b"project", ""))
    return Job(
        at=at, project=found.group(0) if found else "", machine=block.get(b"machine", ""),
        blank=blank, thickness=_number(block.get(b"thickness", "")),
        shrink=_number(block.get(b"shrink factor", "")), items=items,
    )


def parse(lines: Iterable[bytes]) -> Iterator[Job]:
    """Блоки журналу → завдання. Перший блок хвоста буває обрізаний: без дати чи
    диска завдання не складається."""
    block: dict[bytes, str] = {}
    items = 0
    for line in lines:
        if line.startswith(b"item "):
            items += 1  # назву не читаємо: там прізвище пацієнта
            continue
        if line.startswith(b"-----"):
            job = _job(block, items)
            if job is not None:
                yield job
            block, items = {}, 0
            continue
        name, sep, value = line.partition(b":")
        key = name.strip()
        if sep and key in _FIELDS:
            block[key] = value.strip().decode("cp1251", "replace")
    job = _job(block, items)
    if job is not None:
        yield job


def files(db: Session) -> list[str]:
    """Журнали всіх Sum3D, про які знає CRM (теки з Налаштувань і біля дисків)."""
    from app.services import sum3d_deep, sum3d_install
    from app.settings_store import get_setting

    folders = [i.path for i in sum3d_install.read_installs(db)]
    for root in sum3d_deep.settings_roots((get_setting(db, "cam_blanks_path") or "").strip()):
        folders += [root, os.path.join(root, "sum")]
    out: list[str] = []
    seen: set[str] = set()
    for folder in folders:
        path = os.path.join(folder, FILE_NAME)
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            out.append(path)
    return out


def read_tail(path: str, limit: int) -> list[Job]:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        if size > limit:
            handle.readline()  # обірваний рядок
        return list(parse(handle))


def read_all(paths: Iterable[str], limit: int = LEARN_BYTES) -> list[Job]:
    """Усі журнали разом, за часом."""
    jobs: list[Job] = []
    for path in paths:
        try:
            jobs += read_tail(path, limit)
        except OSError as exc:
            if log_throttle.due(f"disc-history:{path}") is not None:
                logger.warning("Журнал Sum3D у %s не прочитано: %s", Path(path).parent, exc)
    jobs.sort(key=lambda j: j.at)
    return jobs


def choices(jobs: Iterable[Job]) -> list[Job]:
    """Один запис на проєкт: диск, на якому він ЛИШИВСЯ. Перерахунок на інший диск
    скасовує перший вибір; на той самий — новим вибором не є (час перший)."""
    last: dict[str, Job] = {}
    loose: list[Job] = []
    for job in jobs:
        if not job.project:
            loose.append(job)
            continue
        was = last.get(job.project)
        if was is None or was.blank != job.blank:
            last[job.project] = job
    return sorted([*last.values(), *loose], key=lambda j: j.at)


# ── Полиця: що зараз із кожним диском ──────────────────────────────────────


@dataclass
class DiscUse:
    first: datetime
    last: datetime
    jobs: int = 0
    items: int = 0


@dataclass
class Shelf:
    """Диски й що з ними робили — станом на останнє враховане завдання."""

    discs: dict[str, DiscUse] = field(default_factory=dict)
    mounted: dict[str, str] = field(default_factory=dict)
    """верстат → диск його останнього завдання."""

    def add(self, job: Job) -> None:
        use = self.discs.get(job.blank)
        if use is None:
            use = self.discs[job.blank] = DiscUse(first=job.at, last=job.at)
        use.last = job.at
        use.jobs += 1
        use.items += job.items
        if job.machine:
            self.mounted[job.machine] = job.blank


def shelf_of(jobs: Iterable[Job]) -> Shelf:
    shelf = Shelf()
    for job in choices(jobs):
        shelf.add(job)
    return shelf


_live: dict[str, tuple[float, tuple, Shelf]] = {}
_live_lock = threading.Lock()


def live_shelf(paths: list[str]) -> Optional[Shelf]:
    """Полиця для картки листа; перечитується, лише коли журнал дописали."""
    if not paths:
        return None
    key = "|".join(paths)
    now = time.monotonic()
    with _live_lock:
        hit = _live.get(key)
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[2]
    stamp: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            stamp.append((path, 0, 0))
            continue
        stamp.append((path, st.st_size, int(st.st_mtime)))
    signature = tuple(stamp)
    shelf = hit[2] if hit and hit[1] == signature else shelf_of(read_all(paths, LIVE_BYTES))
    with _live_lock:
        _live[key] = (now, signature, shelf)
    return shelf


def forget_live() -> None:
    """Для тестів."""
    with _live_lock:
        _live.clear()


def days_between(then: datetime, now: datetime) -> float:
    return max(0.0, (now - then) / timedelta(days=1))
