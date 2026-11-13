"""Справжній диск на плитці табло: обриси робіт із файлу диска Sum3D (`.blk`).

Власник 11–12.11.26: диск на плитці має бути «так, як насправді», а не
вигадана спіраль лунок. Формат розібрано на проді (`kmill_sum3d_files
kind=blk_shape`, диски x88 і x54):

* файл — текстові рядки `<значення>\\r\\n`, а ДВІЙКОВИЙ блок іде одразу після
  рядка `<довжина блоку>`;
* блок обрисів: int32 кількість обрисів C, int32 усього точок P, далі C разів
  int32 n + n точок (x, y, z) у double; z = 0 — середина диска. Перевірка:
  довжина = 8 + 4·C + 24·P і сума n = P (на x54: 6 обрисів, 397 точок, 9560);
* коло диска — окремий запис із n точок на z = ±половина висоти (r = 47).

Обриси лягли на рожеві контури Sum3D точно (звірено з фото власника, x88).
Обриси — це те, що РОЗСТАВЛЕНО на диску: файл переписується, коли оператор
зберігає проєкт, а не коли верстат ріже. Яка частина вже вирізана, файл не
каже.

Диск програми знаходимо за назвою: `4pmma25_25-a2-x276_<дата>_<час>.iso` →
`pmma25_25-a2-x276.blk`, `2_16-Emotions-a3-5-x27_…` → `zr16_16-Emotions-
a3-5-x27.blk` — збігається частина після першого `_`, тека висоти — число
на її початку. Назви файлів дисків — марка й колір, не персональні дані.
"""

from __future__ import annotations

import logging
import math
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_BLANK_BYTES = 4 * 1024 * 1024
_TAG = re.compile(rb"<(\d{1,9})>\r\n")
_DATE_TAIL = re.compile(r"_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:\.iso)?$", re.I)
_COUNT_DASHES = re.compile(r"^\d+-+(\d{1,2}-\S+)$")
_LIST_TTL = 60.0
DEFAULT_RADIUS = 47.0


@dataclass
class BlankShape:
    radius: float
    contours: list[list[tuple[float, float]]] = field(default_factory=list)
    areas: list[float] = field(default_factory=list)

    @property
    def free_share(self) -> float:
        """Частка площі диска поза обрисами (0..1). Обриси — силуети робіт,
        без перемичок і відступів фрези, тож це верхня оцінка вільного."""
        total = math.pi * self.radius ** 2
        return max(0.0, min(1.0, 1 - sum(self.areas) / total)) if total else 0.0


def _area(points: list[tuple[float, float]]) -> float:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def _contour_block(data: bytes, start: int, length: int) -> Optional[list[list[tuple[float, float]]]]:
    """Обриси з блоку за `start` довжиною `length` або None, якщо не він."""
    if length < 8 or start + length > len(data):
        return None
    count, total = struct.unpack_from("<ii", data, start)
    if not (0 <= count <= 5000) or total < 0 or 8 + 4 * count + 24 * total != length:
        return None
    pos, seen, out = start + 8, 0, []
    for _ in range(count):
        (n,) = struct.unpack_from("<i", data, pos)
        pos += 4
        if n < 3 or seen + n > total:
            return None
        values = struct.unpack_from(f"<{3 * n}d", data, pos)
        pos += 24 * n
        seen += n
        zs = values[2::3]
        if max(zs) - min(zs) > 1e-6:
            return None
        out.append(list(zip(values[0::3], values[1::3])))
    return out if seen == total else None


def _radius(data: bytes, after: int) -> float:
    """Коло диска: запис із n точок однакової відстані від центру на z ≠ 0."""
    for match in _TAG.finditer(data, after):
        pos = match.end()
        if pos + 4 > len(data):
            break
        (n,) = struct.unpack_from("<i", data, pos)
        if not (16 <= n <= 4000) or pos + 4 + 24 * n > len(data):
            continue
        values = struct.unpack_from(f"<{3 * n}d", data, pos + 4)
        rs = [math.hypot(x, y) for x, y in zip(values[0::3], values[1::3])]
        if min(rs) > 10 and max(rs) - min(rs) < 0.05 and abs(values[2]) > 0.5:
            return round(sum(rs) / len(rs), 3)
    return DEFAULT_RADIUS


def parse_blank(data: bytes) -> Optional[BlankShape]:
    """Диск із байтів `.blk`; None — формат не той (обрисів не знайдено)."""
    if not data.startswith(b"<SUM3D_BLANK") or len(data) > MAX_BLANK_BYTES:
        return None
    for match in _TAG.finditer(data):
        contours = _contour_block(data, match.end(), int(match.group(1)))
        if contours is None:
            continue
        shape = BlankShape(_radius(data, match.end() + int(match.group(1))))
        shape.contours = contours
        shape.areas = [_area(c) for c in contours]
        return shape
    return None


# ── Диск програми ──────────────────────────────────────────────────────────


def disc_key(program: Optional[str]) -> Optional[str]:
    """`4pmma25_25-a2-x276_2026-…iso` → `25-a2-x276`; без диска — None."""
    text = (program or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:\.iso)?$", text, re.I):
        return None  # гола `дата_час` — диска в назві немає
    head = _DATE_TAIL.sub("", text)
    if "_" not in head:
        # `3--14-Emotions-A3-x861PRO` (250-New, 13.11.26): оператор поставив
        # лічильник через «--» без «_». Лише ця форма: число, дефіси, висота.
        bare = _COUNT_DASHES.match(head)
        return bare.group(1).strip().lower() if bare else None
    key = head.split("_", 1)[1].strip().lower()
    return key if re.match(r"\d{1,2}-", key) else None


_list_cache: dict[str, tuple[float, list[Path]]] = {}
_shape_cache: dict[str, tuple[tuple[int, int], Optional[BlankShape]]] = {}
_lock = threading.Lock()


def _height_dirs(root: Path, height: str) -> list[Path]:
    try:
        return [m / height for m in root.iterdir() if m.is_dir() and (m / height).is_dir()]
    except OSError:
        return []


def _blanks_in(folder: Path) -> list[Path]:
    now = time.monotonic()
    key = str(folder)
    with _lock:
        hit = _list_cache.get(key)
        if hit and now - hit[0] < _LIST_TTL:
            return hit[1]
    try:
        files = [p for p in folder.iterdir() if p.suffix.lower() == ".blk"]
    except OSError:
        files = []
    with _lock:
        _list_cache[key] = (now, files)
    return files


def find_blank(root: Optional[str], program: Optional[str]) -> Optional[Path]:
    """Файл диска програми або None (немає, або кілька однаково схожих)."""
    key = disc_key(program)
    if not root or not key:
        return None
    height = key.split("-", 1)[0]
    found = [p for folder in _height_dirs(Path(root), height) for p in _blanks_in(folder)
             if "_" in p.stem and p.stem.split("_", 1)[1].lower() == key]
    if len(found) > 1:
        # pmma і цирконій можуть мати однаковий хвіст — підказує початок назви.
        pmma = "pmma" in (program or "").split("_", 1)[0].lower()
        found = [p for p in found if p.stem.lower().startswith("pmma") == pmma]
    return found[0] if len(found) == 1 else None


def shape_of(path: Path) -> Optional[BlankShape]:
    """Розібраний диск із кешем за розміром і часом зміни файлу."""
    try:
        stat = path.stat()
    except OSError:
        return None
    mark = (stat.st_size, stat.st_mtime_ns)
    with _lock:
        hit = _shape_cache.get(str(path))
        if hit and hit[0] == mark:
            return hit[1]
    try:
        data = path.read_bytes() if stat.st_size <= MAX_BLANK_BYTES else b""
    except OSError:
        data = b""
    shape = parse_blank(data) if data else None
    with _lock:
        _shape_cache[str(path)] = (mark, shape)
    return shape


def svg_paths(shape: BlankShape, *, r_view: float = 42.0, centre: float = 50.0) -> list[str]:
    """Обриси в координатах `viewBox 0 0 100 100` табло (y угору, як у Sum3D)."""
    k = r_view / shape.radius if shape.radius else 1.0
    out = []
    for contour in shape.contours:
        step = max(1, len(contour) // 60)  # на плитці ~40 px — 60 точок досить
        pts = contour[::step]
        d = "M" + "L".join(f"{centre + x * k:.2f} {centre - y * k:.2f}" for x, y in pts) + "Z"
        out.append(d)
    return out


_root: Optional[str] = None


def configure(blanks_root: Optional[str]) -> None:
    """Тека дисків Sum3D (`cam_blanks_path`); кличе фоновий прохід табло."""
    global _root
    _root = (blanks_root or "").strip() or None


@dataclass
class TileDisc:
    paths: list[str]
    free_pct: int
    name: str


def disc_for_program(program: Optional[str]) -> Optional[TileDisc]:
    """Справжній диск для плитки або None — тоді плитка малює як раніше."""
    path = find_blank(_root, program)
    if path is None:
        return None
    shape = shape_of(path)
    if shape is None:
        return None
    return TileDisc(svg_paths(shape), round(shape.free_share * 100), path.stem)
