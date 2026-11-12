"""Старі диски в теці CAM: які з них, найімовірніше, фізично вже викинуто.

Власник 12.11.26: використаний диск зазвичай просто викидають, а файл `.blk`
лишається в теці Sum3D роками. Гарантії «диска фізично немає» з файлів не
буває — файл не знає про смітник. Тому тут ЛИШЕ ознаки й рівень упевненості,
а рішення за людиною:

* **на верстаті** — назва програми несе диск (`PMMA14_14-A2-X132_…`), і CRM
  пише ці назви з верстатів (`MachineReading.iso_name`). Диск, якого давно
  немає в програмах, давно не брали. Повні назви CORiTEC CRM читає лише з
  жовтня 2026 — до того мовчання верстата ще не доказ;
* **вільно** — частка диска без вирізів (`sum3d_disc.shape_of`): майже
  вирізаний диск і є той, що викидають;
* **коли CRM побачила файл** (`CamBlank.first_seen_at`) — лише для дисків,
  що зʼявились після першого проходу теки (решта — «точка відліку»).

Дата зміни файлу — лише довідково: її зсувають копіювання й антивірус
(`cam_blanks`).

**Лише читання.** Модуль нічого не видаляє й не переносить; стереже
`tests/test_program_folders.py`.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CamBlank, MachineReading
from app.services import sum3d_disc
from app.services.cam_blanks import scan_blanks
from app.services.disc_check import program_disc

# Поріг «майже вирізаний».
NEARLY_USED = 0.15
# Скільки днів без верстата — «давно не брали».
IDLE_DAYS = 30
LONG_IDLE_DAYS = 60
# Читання вмісту дисків — з межею часу: їх десятки тисяч.
SHAPE_BUDGET_S = 20.0


def _disc_id(stem: str) -> tuple[bool, str]:
    """(ПММА?, ключ `14-a2-x132`) — той самий ключ, що в назві програми."""
    low = stem.lower()
    key = low.split("_", 1)[1] if "_" in low else low
    return low.lstrip("0123456789").startswith("pmma"), key


def machine_last_seen(db: Session) -> tuple[dict[tuple[bool, str], datetime], Optional[datetime]]:
    """Коли диск востаннє був у програмі на верстаті + найраніший такий запис."""
    rows = db.execute(
        select(MachineReading.iso_name, func.max(MachineReading.captured_at))
        .where(MachineReading.iso_name.is_not(None))
        .group_by(MachineReading.iso_name)
    ).all()
    seen: dict[tuple[bool, str], datetime] = {}
    for iso, at in rows:
        disc = program_disc(iso)
        if disc is None or at is None:
            continue
        ident = (disc.family == "ПММА", disc.key)
        if ident not in seen or seen[ident] < at:
            seen[ident] = at
    earliest = db.scalar(
        select(func.min(MachineReading.captured_at)).where(MachineReading.iso_name.like("%-%_20__-__-__%"))
    )
    return seen, earliest


@dataclass
class DiscRow:
    rel_path: str
    name: str
    group: str
    last_on_machine: Optional[datetime]
    first_seen: Optional[datetime]
    baseline: bool
    modified: Optional[datetime]
    free: Optional[float] = None
    contours: Optional[int] = None

    def verdict(self, now: datetime) -> str:
        idle = (now - self.last_on_machine).days if self.last_on_machine else None
        if idle is not None and idle < IDLE_DAYS:
            return "в_роботі"
        long_quiet = idle is None or idle >= LONG_IDLE_DAYS
        if self.free is not None and self.free < NEARLY_USED and (idle is None or idle >= IDLE_DAYS):
            return "майже_точно_немає"
        if long_quiet and self.free is not None:
            return "можливо_лежить"
        return "невідомо"

    def reason(self, now: datetime) -> str:
        parts = []
        if self.last_on_machine:
            parts.append(f"на верстаті {(now - self.last_on_machine).days} дн тому")
        else:
            parts.append("на верстаті не бачили")
        if self.free is not None:
            parts.append(f"вільно {round(self.free * 100)} %")
        if self.first_seen and not self.baseline:
            parts.append(f"зʼявився {self.first_seen:%d.%m.%y}")
        if self.modified:
            parts.append(f"файл змінено {self.modified:%d.%m.%y}")
        return ", ".join(parts)


def inventory(db: Session, root: Optional[str], *, sample: int = 40) -> dict[str, Any]:
    """Звіт по всіх дисках теки CAM. Нічого не змінює."""
    started = time.monotonic()
    root = (root or "").strip()
    if not root:
        return {"помилка": "тека дисків CAM не задана («Нові диски» → нижня смуга)"}
    found = scan_blanks(root)
    if not found:
        return {"тека": root, "дисків": 0, "примітка": "файлів .blk не знайдено або тека недоступна"}
    seen, earliest = machine_last_seen(db)
    presence = {
        row.rel_path: row
        for row in db.scalars(select(CamBlank).where(CamBlank.gone_at.is_(None))).all()
    }
    base = Path(root)
    rows: list[DiscRow] = []
    for item in found:
        stem = item.file_name.rsplit(".", 1)[0]
        mark = presence.get(item.rel_path)
        try:
            modified: Optional[datetime] = datetime.fromtimestamp(os.stat(base / item.rel_path).st_mtime)
        except OSError:
            modified = None
        rows.append(DiscRow(
            rel_path=item.rel_path,
            name=stem,
            group=f"{item.material_dir}/{item.height_dir}",
            last_on_machine=seen.get(_disc_id(stem)),
            first_seen=mark.first_seen_at if mark else None,
            baseline=bool(mark and mark.ordered_at == mark.first_seen_at and mark.order_id is None),
            modified=modified,
        ))

    # Вміст читаємо спершу в тих, кого найдовше не бачили на верстаті.
    now = datetime.now()
    order = sorted(rows, key=lambda r: (r.last_on_machine or datetime.min, r.modified or datetime.min))
    read = 0
    for row in order:
        if time.monotonic() - started > SHAPE_BUDGET_S:
            break
        shape = sum3d_disc.shape_of(base / row.rel_path)
        read += 1
        if shape is not None:
            row.free = shape.free_share
            row.contours = len(shape.contours)

    verdicts = Counter(r.verdict(now) for r in rows)
    by_group: dict[str, Counter] = {}
    for r in rows:
        by_group.setdefault(r.group, Counter())[r.verdict(now)] += 1
    seen_buckets = Counter(
        "ніколи" if r.last_on_machine is None
        else "до 30 дн" if (now - r.last_on_machine).days < 30
        else "30–90 дн" if (now - r.last_on_machine).days < 90
        else "понад 90 дн"
        for r in rows
    )
    free_buckets = Counter(
        "не прочитано" if r.free is None
        else "<15 %" if r.free < 0.15
        else "15–50 %" if r.free < 0.5
        else "50–90 %" if r.free < 0.9
        else "≥90 % (майже новий)"
        for r in rows
    )
    candidates = [r for r in rows if r.verdict(now) == "майже_точно_немає"]
    maybe = [r for r in rows if r.verdict(now) == "можливо_лежить"]
    return {
        "тека": root,
        "дисків": len(rows),
        "прочитано_вмісту": read,
        "висновок": dict(verdicts),
        "на_верстаті_востаннє": dict(seen_buckets),
        "вільно": dict(free_buckets),
        "за_текою": {g: dict(c) for g, c in sorted(by_group.items())},
        "історія_назв_з_верстатів_від": earliest.strftime("%Y-%m-%d") if earliest else None,
        "майже_точно_немає_приклади": [
            {"диск": r.name, "тека": r.group, "чому": r.reason(now)} for r in candidates[:sample]
        ],
        "можливо_лежить_приклади": [
            {"диск": r.name, "тека": r.group, "чому": r.reason(now)} for r in maybe[:sample]
        ],
        "прохід_с": round(time.monotonic() - started, 1),
        "пояснення": (
            f"«майже_точно_немає» — вільно <{round(NEARLY_USED * 100)} % і ≥{IDLE_DAYS} дн не було на "
            f"верстаті; «можливо_лежить» — ≥{LONG_IDLE_DAYS} дн без верстата, місце ще є; "
            f"«в_роботі» — на верстаті за останні {IDLE_DAYS} дн. Вміст читається з межею "
            f"{SHAPE_BUDGET_S:g} с, найстаріші першими — решта «невідомо». Нічого не змінює."
        ),
    }
