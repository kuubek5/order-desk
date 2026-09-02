"""Захоплення нових проєктів Sum3D із теки «Cam-work» (хід 1 ROADMAP_IDEAS.md).

Sum3D іменує теку проєкту як `YYYY-MM-DD_HH-MM-SS` (дата + точний час). Оператори
вписують у CRM лише хвіст `HH-MM-SS` — це і є Sum3D ID. CRM сканує теку й показує
найновіші ID у шапці черги, щоб не переписувати їх вручну.

Модуль ЧИТАЄ теку й нічого в неї не пише. Без Request/Response і без БД: чиста
логіка, придатна до юніт-тестів. Головна властивість — час зашитий у самій назві,
тому затримка сканування не спотворює момент створення.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# `2026-09-02_17-55-28` (можливий суфікс на кшталт ` (2)` чи `_repeat` ігнорується
# — беремо саме дату+час на початку назви). Хвіст HH-MM-SS = Sum3D ID.
_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})[_ ](?P<time>\d{2}-\d{2}-\d{2})")


@dataclass(frozen=True)
class CapturedProject:
    sum3d_id: str      # хвіст HH-MM-SS — те, що йде в таблицю
    date: str          # YYYY-MM-DD з назви
    folder: str        # повна назва теки (для діагностики/майбутнього)
    mtime: float       # час зміни на диску — лише для сортування


def parse_project_name(name: str) -> tuple[str, str] | None:
    """Назва теки → (date, sum3d_id) або None, якщо не схожа на проєкт Sum3D.

    Приклади: `2026-09-02_17-55-28` → ('2026-09-02', '17-55-28').
    Не-проєктні теки (тимчасові, службові) відкидаються тихо.
    """
    m = _NAME_RE.match(name.strip())
    if not m:
        return None
    return m.group("date"), m.group("time")


def scan_projects(path: str | None, *, limit: int = 12) -> list[CapturedProject]:
    """Найновіші проєкти Sum3D у теці, впорядковані від найновішого.

    Порожній/невказаний шлях або недоступна тека → порожній список (функція
    просто вимкнена, не помилка). Сканує лише перший рівень: проєкти лежать
    теками прямо в Cam-work.
    """
    value = (path or "").strip()
    if not value:
        return []
    root = Path(value)
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return []

    found: list[CapturedProject] = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            parsed = parse_project_name(entry.name)
            if parsed is None:
                continue
            date, sid = parsed
            found.append(
                CapturedProject(
                    sum3d_id=sid, date=date, folder=entry.name,
                    mtime=entry.stat().st_mtime,
                )
            )
        except (OSError, PermissionError):
            continue

    # Найновіші першими. При однаковому mtime — за назвою (стабільно).
    found.sort(key=lambda p: (p.mtime, p.folder), reverse=True)
    return found[:limit]
