"""Слід різання програми верстата: де саме на диску ріже ця `.iso`.

Власник 14.11.26: на табло підсвічувати обриси, що фрезеруються ЗАРАЗ, і
«механізм має визначати точно». Найточніше джерело — сама програма: у G-коді
записані координати руху фрези в мм від центру диска (звірено на двох `.iso`
у теці Sum3D на проді). Проєкт `.cam` для цього ненадійний (`disc_link`, три
верстати — три різні відповіді).

Звідки програми. Теку виводу кожного верстата Sum3D тримає в
`<тека Sum3D>\\Machines.arl` (`DirPostProcess`). Старі верстати: `.iso` лягає в
`C:\\CAM-out\\…` на ПК із Sum3D ненадовго — потім верстат ПЕРЕНОСИТЬ її до себе
(власник 14.11.26). Тому слід знімається одразу після появи файлу й лежить у
власному кеші KuubMill (`DATA_DIR/program_tracks`), а не читається з теки під
час різання. Нові верстати пишуть у свою мережеву теку `…\\SmartControl\\Jobs`.

Лише читання, і через `shared_read.open_shared`: верстат мусить мати змогу
забрати чи стерти файл, поки ми його читаємо. У теки Sum3D і верстатів нічого
не пишеться. Назв об'єктів (`;Object:` — STL, там бувають прізвища) не
зберігаємо — лише їх кількість.

Що зберігаємо: клітинки 0,5 мм, де фреза різала прямо згори чи знизу
(B = 0, A = 0 або 180) на висоті диска. Нахилені проходи 5 осей у площину
диска напряму не лягають — їх пропускаємо: кожну роботу все одно обдирають з
обох боків без нахилу.

Звірка з диском (`match`) сама себе перевіряє: частка клітинок сліду, що
лягла на обриси `.blk`. Знак осей для кожного боку підбирається з чотирьох
варіантів — який ліг найкраще. Мало лягло — «ненадійно», і табло тоді нічого
не підсвічує: хибна підсвітка гірша за жодну.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from app import log_throttle
from app.shared_read import open_shared

logger = logging.getLogger(__name__)

CELL_MM = 0.5
# Вище/нижче цього фреза не ріже диск (найвищий диск 25 мм → ±12,5).
MAX_CUT_Z = 14.0
MAX_FILE_BYTES = 60 * 1024 * 1024
# Файл молодший за це ще може дописуватись.
SETTLE_SECONDS = 2.0
# Скільки днів назад дивитись у теку і скільки тримати слід у кеші.
RECENT_DAYS = 3
KEEP_DAYS = 14
MAX_FILES_PER_PASS = 4
FOLDER_BACKOFF_SECONDS = 600.0
FOLDERS_TTL_SECONDS = 300.0
PROGRAM_EXTS = (".iso", ".nc")
# Обрис «свій», коли слід покрив хоча б таку його частку (і не менше клітинок).
OWN_SHARE = 0.15
OWN_MIN_CELLS = 8
# Фреза обходить роботу ззовні на свій радіус і припуск.
NEAR_MM = 3.0
# Слід вважаємо надійним, коли стільки його лягло на обриси.
TRUST_SHARE = 0.85

_WORD = re.compile(rb"([GXYZAB])\s*(-?\d+(?:\.\d*)?|-?\.\d+)")
_TAIL = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$")
_FLIPS = ((1, 1), (1, -1), (-1, 1), (-1, -1))


# ── Теки програм із Sum3D ──────────────────────────────────────────────────


@dataclass(frozen=True)
class MachineFolder:
    machine: str
    path: str
    ext: str

    @property
    def is_network(self) -> bool:
        return self.path.startswith("\\\\")


def parse_machines(text: str) -> list[MachineFolder]:
    """Верстати з `Machines.arl`: назва, тека виводу, розширення програми."""
    out: list[MachineFolder] = []
    name = path = ext = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line == "<MACHINESTART>":
            name = path = ext = ""
        elif line.startswith("Machine="):
            name = line[8:].strip()
        elif line.startswith("DirPostProcess="):
            path = line[15:].strip().rstrip("\\")
        elif line.startswith("ExtPostProcess="):
            ext = line[15:].strip().lower()
        elif line == "<MACHINEEND>":
            if name and path:
                out.append(MachineFolder(name, path, ext or ".iso"))
            name = path = ext = ""
    return out


def machine_folders(install_paths: Iterable[str]) -> list[MachineFolder]:
    """Теки виводу з усіх `Machines.arl` встановлень Sum3D. Не знайдено — []."""
    out: list[MachineFolder] = []
    for root in install_paths:
        target = os.path.join(root, "Machines.arl")
        try:
            with open_shared(target) as handle:
                text = handle.read(2 * 1024 * 1024).decode("latin-1", "replace")
        except OSError as exc:
            skipped = log_throttle.due(f"program-tracks-arl:{target}")
            if skipped is not None:
                logger.warning("Слід програми: %s не прочитано (%s; ще %d разів відтоді)",
                               target, exc.__class__.__name__, skipped)
            continue
        out.extend(parse_machines(text))
    return out


# ── Розбір програми ────────────────────────────────────────────────────────


@dataclass
class Track:
    name: str
    blank: str = ""
    objects: int = 0
    lines: int = 0
    # Клітинки (x, y) у пів-міліметрах для кожного боку: «0» і «180».
    sides: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    skipped_tilted: int = 0
    source: str = ""
    file_mtime: float = 0.0
    file_size: int = 0
    captured_at: str = ""

    @property
    def cells(self) -> int:
        return sum(len(v) for v in self.sides.values())


def parse_program(data: bytes, name: str = "") -> Track:
    """G-код → слід. Модальні G/X/Y/Z/A/B; береться лише різання (G1) без
    нахилу на висоті диска."""
    track = Track(name=name)
    x = y = z = None
    a = b = 0.0
    cutting = False
    cells: dict[str, set[tuple[int, int]]] = {"0": set(), "180": set()}
    for line in data.splitlines():
        track.lines += 1
        if not line:
            continue
        head = line[:1]
        if head == b";":
            low = line[:40].lower()
            if low.startswith(b";blank:"):
                track.blank = line[7:].strip().decode("latin-1", "replace")[:120]
            elif low.startswith(b";object:"):
                track.objects += 1
            continue
        if head not in b"GXYZAB":
            continue
        moved = False
        for word, value in _WORD.findall(line):
            number = float(value)
            if word == b"G":
                if number in (0.0, 1.0):
                    cutting = number == 1.0
            elif word == b"X":
                x, moved = number, True
            elif word == b"Y":
                y, moved = number, True
            elif word == b"Z":
                z, moved = number, True
            elif word == b"A":
                a = number
            elif word == b"B":
                b = number
        if not (moved and cutting) or x is None or y is None or z is None or abs(z) > MAX_CUT_Z:
            continue
        turn = a % 360.0
        if abs(b) > 0.01 or min(abs(turn), abs(turn - 180.0), abs(turn - 360.0)) > 0.01:
            track.skipped_tilted += 1
            continue
        side = "180" if abs(turn - 180.0) <= 0.01 else "0"
        cells[side].add((int(round(x / CELL_MM)), int(round(y / CELL_MM))))
    track.sides = {k: sorted(v) for k, v in cells.items() if v}
    return track


# ── Кеш слідів ─────────────────────────────────────────────────────────────


def cache_dir() -> Path:
    from app.config import DATA_DIR

    return DATA_DIR / "program_tracks"


def track_key(program: Optional[str]) -> str:
    """Назва програми без розширення, малими — ключ кешу й пошуку."""
    stem = (program or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(iso|nc)$", "", stem, flags=re.I)
    return re.sub(r"[^a-z0-9._+()\-]", "_", stem.lower())[:180]


def save_track(track: Track, folder: Optional[Path] = None) -> Path:
    folder = folder or cache_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{track_key(track.name)}.json"
    payload = {
        "name": track.name, "blank": track.blank, "objects": track.objects, "lines": track.lines,
        "sides": track.sides, "skipped_tilted": track.skipped_tilted, "source": track.source,
        "file_mtime": track.file_mtime, "file_size": track.file_size, "captured_at": track.captured_at,
    }
    part = target.with_suffix(".part")
    part.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(part, target)
    return target


def _load(path: Path) -> Optional[Track]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    sides = {str(k): [(int(p[0]), int(p[1])) for p in v] for k, v in (raw.get("sides") or {}).items()}
    return Track(
        name=str(raw.get("name") or ""), blank=str(raw.get("blank") or ""),
        objects=int(raw.get("objects") or 0), lines=int(raw.get("lines") or 0), sides=sides,
        skipped_tilted=int(raw.get("skipped_tilted") or 0), source=str(raw.get("source") or ""),
        file_mtime=float(raw.get("file_mtime") or 0), file_size=int(raw.get("file_size") or 0),
        captured_at=str(raw.get("captured_at") or ""),
    )


def find_track(program: Optional[str], folder: Optional[Path] = None) -> Optional[Track]:
    """Слід програми за назвою з екрана верстата. Точна назва, інакше — єдиний
    файл із тим самим хвостом «дата_час» (екран нового верстата ріже початок)."""
    folder = folder or cache_dir()
    key = track_key(program)
    if not key:
        return None
    exact = folder / f"{key}.json"
    if exact.is_file():
        return _load(exact)
    tail = _TAIL.search(key.rstrip("_"))
    if tail is None:
        return None
    try:
        same = [p for p in folder.glob(f"*{tail.group(1)}*.json")]
    except OSError:
        return None
    return _load(same[0]) if len(same) == 1 else None


def prune(folder: Optional[Path] = None, *, now: Optional[float] = None) -> int:
    folder = folder or cache_dir()
    limit = (now or time.time()) - KEEP_DAYS * 86400
    removed = 0
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return 0
    for path in entries:
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ── Збирач ─────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_folder_state: dict[str, dict[str, Any]] = {}
_backoff_until: dict[str, float] = {}
_folders_cache: tuple[float, list[MachineFolder]] = (0.0, [])
_last_prune = 0.0


def folder_status() -> dict[str, dict[str, Any]]:
    """Що збирач бачив у теках востаннє (для діагностики)."""
    with _lock:
        return {k: dict(v) for k, v in _folder_state.items()}


def known_folders(db) -> list[MachineFolder]:
    """Теки з `Machines.arl` плюс «Теки програм» з Налаштувань; кеш 5 хв."""
    global _folders_cache
    now = time.monotonic()
    with _lock:
        stamp, cached = _folders_cache
        if cached and now - stamp < FOLDERS_TTL_SECONDS:
            return cached
    from sqlalchemy import select

    from app.models import Machine
    from app.services.sum3d_install import read_installs

    found = machine_folders(i.path for i in read_installs(db))
    for name, path in db.execute(select(Machine.name, Machine.program_folder)).all():
        if (path or "").strip():
            found.append(MachineFolder(name, path.strip().rstrip("\\"), ".iso"))
    with _lock:
        _folders_cache = (now, found)
    return found


def _complete(path: str, size: int) -> bool:
    """Програма дописана: наприкінці є M30 (кінець програми)."""
    try:
        with open_shared(path) as handle:
            handle.seek(max(0, size - 400))
            return b"M30" in handle.read(400).upper()
    except OSError:
        return False


def collect_folder(path: str, *, target: Optional[Path] = None, now: Optional[float] = None,
                   budget: int = MAX_FILES_PER_PASS) -> int:
    """Один прохід теки: зняти слід зі свіжих програм, яких ще немає в кеші.
    Повертає, скільки знято. Теку лише читає."""
    target = target or cache_dir()
    moment = now or time.time()
    state: dict[str, Any] = {"перевірено": datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S")}
    try:
        with os.scandir(path) as it:
            entries = [(e.name, e.stat()) for e in it
                       if e.name.lower().endswith(PROGRAM_EXTS) and e.is_file()]
    except OSError as exc:
        state["помилка"] = exc.__class__.__name__
        with _lock:
            _folder_state[path] = state
            _backoff_until[path] = time.monotonic() + FOLDER_BACKOFF_SECONDS
        return 0
    state["програм"] = len(entries)
    entries.sort(key=lambda item: item[1].st_mtime, reverse=True)
    if entries:
        state["найновіша"] = datetime.fromtimestamp(entries[0][1].st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    taken = 0
    for name, stat in entries:
        if taken >= budget or moment - stat.st_mtime > RECENT_DAYS * 86400:
            break
        if moment - stat.st_mtime < SETTLE_SECONDS or not (0 < stat.st_size <= MAX_FILE_BYTES):
            continue
        cached = target / f"{track_key(name)}.json"
        try:
            if cached.is_file() and cached.stat().st_mtime >= stat.st_mtime:
                continue
        except OSError:
            pass
        full = os.path.join(path, name)
        if not _complete(full, stat.st_size):
            continue
        try:
            with open_shared(full) as handle:
                data = handle.read(MAX_FILE_BYTES)
        except OSError as exc:
            skipped = log_throttle.due(f"program-tracks-read:{path}")
            if skipped is not None:
                logger.warning("Слід програми: файл у %s не прочитано (%s; ще %d разів відтоді)",
                               path, exc.__class__.__name__, skipped)
            continue
        track = parse_program(data, name)
        track.source = path
        track.file_mtime, track.file_size = stat.st_mtime, stat.st_size
        track.captured_at = datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M:%S")
        save_track(track, target)
        taken += 1
        logger.info("Слід програми: %s → %d клітинок (без нахилу), пропущено нахилених %d",
                    name, track.cells, track.skipped_tilted)
    state["знято_за_прохід"] = taken
    with _lock:
        _folder_state[path] = state
    return taken


def collect_once(db, *, network: bool) -> int:
    """Прохід збирача по локальних АБО мережевих теках (окремі потоки: мертва
    мережева тека висить на таймаутах і не має затримувати локальні, звідки
    верстат забирає файл)."""
    global _last_prune
    taken = 0
    seen: set[str] = set()
    for item in known_folders(db):
        key = item.path.lower()
        if item.is_network != network or key in seen:
            continue
        seen.add(key)
        with _lock:
            wait = _backoff_until.get(item.path, 0.0)
        if time.monotonic() < wait:
            continue
        taken += collect_folder(item.path)
    if not network and time.monotonic() - _last_prune > 3600:
        _last_prune = time.monotonic()
        prune()
    return taken


# ── Звірка з диском ────────────────────────────────────────────────────────


def match(track: Track, radius: float, contours: list[list[tuple[float, float]]]) -> dict[str, Any]:
    """Які обриси диска ріже ця програма і наскільки цьому можна вірити."""
    from app.services.disc_fit import _dilate, _fill_polygon

    n = int(math.ceil(2 * radius / CELL_MM))
    origin = -radius
    label = np.zeros((n, n), dtype=np.int32)
    areas: list[int] = []
    for index, poly in enumerate(contours):
        one = np.zeros((n, n), dtype=bool)
        _fill_polygon(one, poly, origin)
        areas.append(int(one.sum()))
        label[one] = index + 1
    grown = _dilate(label > 0, int(round(NEAR_MM / CELL_MM)))
    pad = (grown.shape[0] - n) // 2
    near = grown[pad: pad + n, pad: pad + n]

    counts = np.zeros(len(contours) + 1, dtype=np.int64)
    total = on_disc = close = 0
    report_sides: dict[str, Any] = {}
    for side, cells in track.sides.items():
        pts = np.array(cells, dtype=np.float64) * CELL_MM
        best: Optional[tuple[float, int, int, np.ndarray, tuple[int, int]]] = None
        for sx, sy in _FLIPS:
            col = np.floor((pts[:, 0] * sx - origin) / CELL_MM).astype(np.int64)
            row = np.floor((pts[:, 1] * sy - origin) / CELL_MM).astype(np.int64)
            ok = (col >= 0) & (col < n) & (row >= 0) & (row < n)
            hits = int(near[row[ok], col[ok]].sum())
            share = hits / len(cells) if cells else 0.0
            if best is None or share > best[0]:
                best = (share, hits, int(ok.sum()), label[row[ok], col[ok]], (sx, sy))
        assert best is not None
        share, hits, inside, labels, flip = best
        total += len(cells)
        on_disc += inside
        close += hits
        counts += np.bincount(labels, minlength=len(contours) + 1)
        report_sides[side] = {"клітинок": len(cells), "знак_xy": list(flip), "лягло_на_обриси": round(share, 3)}
    own = [i + 1 for i, area in enumerate(areas)
           if counts[i + 1] >= max(OWN_MIN_CELLS, OWN_SHARE * area)]
    share_all = close / total if total else 0.0
    return {
        "обрисів": len(contours),
        "свої_обриси": own,
        "покриття_обрисів": {str(i + 1): round(float(counts[i + 1]) / a, 2) for i, a in enumerate(areas)
                             if a and counts[i + 1]},
        "клітинок_сліду": total,
        "лягло_на_обриси": round(share_all, 3),
        "надійно": bool(own) and share_all >= TRUST_SHARE,
        "боки": report_sides,
    }
