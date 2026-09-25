"""Однакові файли в одному листі — підозра, що клієнт надіслав роботу двічі.

Бойовий випадок (власник 25.09.26): два STL прикріплені окремо і ті самі два —
ще й в архіві. Розпакування кладе копії поруч («crown (2).stl»), а цей модуль
каже картці, що саме однакове, щоб оператор не прийняв і не відфрезерував одну
роботу двічі. Модуль лише ПІДКАЗУЄ (§2 «рішення за оператором»): копії лише не
позначаються галочкою за замовчуванням, нічого не видаляється.

Два різні сигнали:
  * однаковий ВМІСТ (байт-у-байт, будь-які імена) — «дубль»;
  * однакова НАЗВА (без « (2)»), але різний вміст — «інший вміст»: можливо,
    виправлена версія, питання до клієнта.

Хеш рахуємо лише для файлів ОДНАКОВОГО розміру: файли лежать на мережевій
шарі, і читати кожен на кожне відкриття картки — зайві мегабайти.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_COPY_SUFFIX = re.compile(r"^(?P<stem>.*?) \((?P<n>\d+)\)$")
_CHUNK = 1024 * 1024


@dataclass
class DuplicateReport:
    """`copy_of` — {id копії: імʼя оригіналу} (копію не позначаємо за
    замовчуванням); `same_content` — групи імен з однаковим вмістом;
    `name_conflicts` — імена, що збігаються, а вміст різний; `conflict_ids` —
    файли з таких груп (для бейджа)."""

    copy_of: dict[int, str] = field(default_factory=dict)
    same_content: list[list[str]] = field(default_factory=list)
    name_conflicts: list[str] = field(default_factory=list)
    conflict_ids: set[int] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.copy_of or self.name_conflicts)


def base_name(filename: str) -> str:
    """«crown (2).stl» → «crown.stl» (регістр не важить)."""
    path = Path(filename)
    match = _COPY_SUFFIX.match(path.stem)
    stem = match.group("stem") if match else path.stem
    return f"{stem}{path.suffix}".lower()


def _digest(path: Path) -> str | None:
    try:
        h = hashlib.sha1()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _size(attachment) -> int | None:
    if attachment.size_bytes:
        return int(attachment.size_bytes)
    try:
        return Path(attachment.saved_path).stat().st_size
    except OSError:
        return None


# Кеш звіту для КАРТКИ: вона кличе find_duplicates на кожне відкриття, а читання
# вмісту однакових за розміром файлів на мережевому спулі — секунди. Ключ той
# самий, що в `_LIST_CACHE` (id, шлях, розмір, імʼя): інший набір файлів — інший
# ключ. Звіт не змінюють після побудови, тож віддавати той самий обʼєкт безпечно.
_REPORT_CACHE: dict[tuple, DuplicateReport] = {}
_REPORT_CACHE_MAX = 256


def find_duplicates(attachments: list) -> DuplicateReport:
    """`attachments` — нерозібрані файли листа, що є на диску (обʼєкти з `id`,
    `filename`, `saved_path`, `size_bytes`). Оригінал групи — найменший `id`
    (вкладене окремо приходить раніше за розпаковане з архіву)."""
    key = tuple(sorted(
        (a.id, a.saved_path or "", a.size_bytes or 0, a.filename or "")
        for a in attachments
    ))
    cached = _REPORT_CACHE.get(key)
    if cached is not None:
        return cached
    report = _build_report(attachments)
    if len(_REPORT_CACHE) >= _REPORT_CACHE_MAX:
        _REPORT_CACHE.clear()
    _REPORT_CACHE[key] = report
    return report


def _build_report(attachments: list) -> DuplicateReport:
    report = DuplicateReport()
    ordered = sorted(attachments, key=lambda a: a.id)

    by_size: dict[int, list] = defaultdict(list)
    for attachment in ordered:
        size = _size(attachment)
        if size:
            by_size[size].append(attachment)
    digest_of: dict[int, str] = {}
    for group in by_size.values():
        if len(group) < 2:
            continue
        for attachment in group:
            digest = _digest(Path(attachment.saved_path))
            if digest:
                digest_of[attachment.id] = digest

    by_digest: dict[str, list] = defaultdict(list)
    for attachment in ordered:
        if attachment.id in digest_of:
            by_digest[digest_of[attachment.id]].append(attachment)
    for group in by_digest.values():
        if len(group) < 2:
            continue
        original = group[0]
        for copy in group[1:]:
            report.copy_of[copy.id] = original.filename
        report.same_content.append([a.filename for a in group])

    by_name: dict[str, list] = defaultdict(list)
    for attachment in ordered:
        by_name[base_name(attachment.filename)].append(attachment)
    for group in by_name.values():
        if len(group) < 2:
            continue
        digests = {digest_of.get(a.id) for a in group}
        if len(digests) == 1 and None not in digests:
            continue  # вся група однакова — це вже «дубль», не конфлікт
        different = [a for a in group if a.id not in report.copy_of]
        if len(different) < 2:
            continue
        report.name_conflicts.append(group[0].filename)
        report.conflict_ids.update(a.id for a in different)
    return report


# Кеш для СПИСКУ листів (полл кожні 15 с): ключ — набір файлів (id, шлях,
# розмір), тож новий/розпакований файл дає новий ключ. Вміст читається лише
# там, де є файли однакового розміру, і лише раз на набір.
_LIST_CACHE: dict[tuple, bool] = {}
_LIST_CACHE_MAX = 512


def has_duplicate_files(attachments: list) -> bool:
    """Чи надіслав клієнт ту саму роботу двічі — для бейджа в списку листів
    (власник 25.09.26: зелена галочка на такому листі вводила в оману).
    Лише однаковий ВМІСТ (`copy_of`); тезки з різним вмістом — у картці."""
    pending = [a for a in attachments if getattr(a, "order_id", None) is None]
    if len(pending) < 2:
        return False
    key = tuple(sorted((a.id, a.saved_path or "", a.size_bytes or 0) for a in pending))
    cached = _LIST_CACHE.get(key)
    if cached is not None:
        return cached
    result = bool(find_duplicates(pending).copy_of)
    if len(_LIST_CACHE) >= _LIST_CACHE_MAX:
        _LIST_CACHE.clear()
    _LIST_CACHE[key] = result
    return result
