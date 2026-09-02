"""Захоплення нових проєктів Sum3D із теки «Cam-work» (хід 1 ROADMAP_IDEAS.md).

Sum3D іменує проєкт як `YYYY-MM-DD_HH-MM-SS` (дата + точний час). Оператори
вписують у CRM лише хвіст `HH-MM-SS` — це і є Sum3D ID. CRM сканує теку й показує
найновіші ID у шапці черги, щоб не переписувати їх вручну.

**Реальність цеху (перевірено на D:\\CAM-work):** проєкт — це ФАЙЛ `*.cam`, не
тека. Windows при дублюванні дописує «— копия»/«— копия (2)» до імені, але
дата+час на початку лишаються — з них і беремо ID, а дублікати збігаються в
один. Теку-проєкт (як у первісному роадмапі) теж приймаємо — на випадок іншого
налаштування Sum3D; будь-який інший файл (звіт, тимчасовий) відкидаємо тихо.

Модуль ЧИТАЄ теку й нічого в неї не пише. Без Request/Response і без БД: чиста
логіка, придатна до юніт-тестів. Головна властивість — час зашитий у самій назві,
тому затримка сканування не спотворює момент створення.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# `2026-09-02_17-55-28` (можливий суфікс на кшталт ` (2)`, ` — копия` чи `_repeat`
# ігнорується — беремо саме дату+час на початку назви). Хвіст HH-MM-SS = Sum3D ID.
_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})[_ ](?P<time>\d{2}-\d{2}-\d{2})")

# Розширення файлу-проєкту Sum3D. Порівняння — регістронезалежне (.cam/.CAM).
_PROJECT_EXT = ".cam"


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


def _project_label(entry: Path) -> str | None:
    """Ім'я для розбору, або None якщо запис — не проєкт Sum3D.

    Файл `*.cam` → ім'я без розширення (реальний формат цеху). Тека → її ім'я
    (запасний формат). Будь-який інший файл (звіт, `.stl`, тимчасовий) → None.
    is_dir()/is_file() тихо ловлять биті симлінки/гонки видалення.
    """
    try:
        if entry.is_dir():
            return entry.name
        if entry.is_file() and entry.suffix.lower() == _PROJECT_EXT:
            return entry.stem
    except (OSError, PermissionError):
        return None
    return None


def scan_projects(path: str | None, *, limit: int = 12) -> list[CapturedProject]:
    """Найновіші проєкти Sum3D у теці, впорядковані від найновішого.

    Порожній/невказаний шлях або недоступна тека → порожній список (функція
    просто вимкнена, не помилка). Сканує лише перший рівень: проєкти лежать
    файлами `*.cam` (або теками) прямо в Cam-work.

    Windows-дублікати одного проєкту («2025-09-29_10-30-36» і «… — копия»)
    несуть той самий дата+час → згортаються в один запис (найсвіжіший mtime).
    Без цього «+N» у лотку роздувався б копіями. Ключ дедупу — (date, id):
    той самий час у різні дні лишається різними проєктами.
    """
    value = (path or "").strip()
    if not value:
        return []
    root = Path(value)
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return []

    # Дедуп за (date, sum3d_id): дублікати-копії злити, лишити найсвіжіший.
    best: dict[tuple[str, str], CapturedProject] = {}
    for entry in entries:
        label = _project_label(entry)
        if label is None:
            continue
        parsed = parse_project_name(label)
        if parsed is None:
            continue
        date, sid = parsed
        try:
            mtime = entry.stat().st_mtime
        except (OSError, PermissionError):
            continue
        key = (date, sid)
        prev = best.get(key)
        if prev is None or mtime > prev.mtime:
            best[key] = CapturedProject(
                sum3d_id=sid, date=date, folder=entry.name, mtime=mtime,
            )

    found = list(best.values())
    # Найновіші першими. При однаковому mtime — за назвою (стабільно).
    found.sort(key=lambda p: (p.mtime, p.folder), reverse=True)
    return found[:limit]
