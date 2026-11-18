"""Чи справді влізуть конструкції листа на диск — розстановкою, а не сумою площ.

Власник 14.11.26: «перевіряти, чи найбільша конструкція влазить у найбільшу
вільну ділянку диска, а не лише в суму площ». Сума площ (`disc_suggest`,
`PACKING`) казала «влазить», коли 20 % вільного лежали клаптиками між
вирізами, куди міст фізично не ляже.

Як рахуємо (сітка `GRID_MM`):

* диск — коло з `.blk` (`sum3d_disc.BlankShape`) мінус край `RIM_MM`; зайняте —
  обриси вже розставлених робіт (як вони є у файлі Sum3D);
* конструкція — СИЛУЕТ зі STL exocad (`…_cad.stl`, повернутий до осі
  введення: площина XY = площина диска), помножений на усадку, роздутий на
  `CLEARANCE_MM` — місце для фрези й тримачів між роботами;
* ставимо по черзі, від найбільшої, у кількох поворотах (`ANGLES`), ближче до
  краю диска (центр лишається вільним для наступних). Не стала хоч одна — не
  влазить.

Це НАБЛИЖЕННЯ: Sum3D повертає точніше й знає свої тримачі. Тому «влазить» тут
означає «знайшлось місце з запасом», а остаточно розставляє Sum3D. Лише
читання файлів.
"""

from __future__ import annotations

import math
import os
import re
import threading
from pathlib import Path
from typing import Optional

import numpy as np

GRID_MM = 0.5
# Проміжок навколо кожної нової конструкції: фреза 2,5 мм іде між роботами,
# плюс тримачі. Менше — «влазить» там, де Sum3D уже не поставить.
CLEARANCE_MM = 2.8  # власник 14.11.26, бліц (було 2 мм)
# Край диска, куди роботи не ставлять. Коло у `.blk` — уже робоча зона Sum3D
# (r 47 на диску 98), тож запас малий (14.11.26: 2 мм відсіювали забагато).
RIM_MM = 1.0
# Наскільки запас нової роботи може зайти на вже вирізану дірку (рожевий
# контур Sum3D — це дірка в диску): власник 18.11.26, «трішечки», до 1 мм.
# Лише на старі дірки: між новими роботами листа й до краю диска — повний запас.
HOLE_OVERLAP_MM = 1.0
ANGLES = (0, 90, 45, 135, 30, 60, 120, 150)
_MAX_POINTS = 400_000

_STL_FACET = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
_ASCII_VERTEX = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)")

_lock = threading.Lock()
_points_cache: dict[str, tuple[tuple[int, int], Optional[np.ndarray]]] = {}


# ── Конструкція ─────────────────────────────────────────────────────────────


def stl_points(path: str) -> Optional[np.ndarray]:
    """Точки STL у площині XY (мм), з кешем за розміром і часом зміни."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    stamp = (st.st_size, int(st.st_mtime))
    with _lock:
        hit = _points_cache.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    points: Optional[np.ndarray] = None
    try:
        data = Path(path).read_bytes()
    except OSError:
        data = b""
    if len(data) >= 84:
        count = int(np.frombuffer(data, "<u4", 1, 80)[0])
        if count and 84 + count * 50 == len(data):
            facets = np.frombuffer(data, _STL_FACET, count, 84)
            points = facets["v"][:, :, :2].reshape(-1, 2).astype(np.float64)
    if points is None and data[:5].lower() == b"solid":
        found = _ASCII_VERTEX.findall(data)
        if found:
            points = np.array([(float(x), float(y)) for x, y, _z in found], dtype=np.float64)
    if points is not None and len(points) > _MAX_POINTS:
        points = points[:: math.ceil(len(points) / _MAX_POINTS)]
    with _lock:
        _points_cache[path] = (stamp, points)
    return points


def _disk_offsets(radius_cells: int) -> list[tuple[int, int]]:
    r = radius_cells
    return [(dy, dx) for dy in range(-r, r + 1) for dx in range(-r, r + 1) if dy * dy + dx * dx <= r * r]


def _dilate(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    if radius_cells <= 0:
        return mask
    r = radius_cells
    padded = np.zeros((mask.shape[0] + 2 * r, mask.shape[1] + 2 * r), dtype=bool)
    for dy, dx in _disk_offsets(r):
        padded[r + dy: r + dy + mask.shape[0], r + dx: r + dx + mask.shape[1]] |= mask
    return padded


def piece_mask(points: np.ndarray, angle: float, scale: float = 1.0) -> np.ndarray:
    """Силует конструкції на сітці: точки, повернуті на `angle`, роздуті на
    `CLEARANCE_MM` (це ж закриває проміжки між вершинами сітки STL)."""
    a = math.radians(angle)
    rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    pts = (points * scale) @ rot.T
    pts -= pts.min(axis=0)
    cells = np.floor(pts / GRID_MM).astype(np.int64)
    h, w = int(cells[:, 1].max()) + 1, int(cells[:, 0].max()) + 1
    mask = np.zeros((h, w), dtype=bool)
    mask[cells[:, 1], cells[:, 0]] = True
    # Проміжки між вершинами закриває роздування на CLEARANCE; замкнені дірки
    # всередині силуету — заливка. Рядки цілком НЕ заливаємо: дугу мосту це
    # роздувало до суцільного прямокутника (14.11.26: «майже не знайшов дисків»).
    return _fill_holes(_dilate(mask, math.ceil(CLEARANCE_MM / GRID_MM)))


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Залити порожнини, до яких не дістатись ззовні (вгнутість лишається)."""
    outside = np.zeros((mask.shape[0] + 2, mask.shape[1] + 2), dtype=bool)
    open_ = np.ones_like(outside)
    open_[1:-1, 1:-1] = ~mask
    outside[0, :] = True
    outside[-1, :] = True
    outside[:, 0] = True
    outside[:, -1] = True
    while True:
        grown = outside.copy()
        grown[1:, :] |= outside[:-1, :]
        grown[:-1, :] |= outside[1:, :]
        grown[:, 1:] |= outside[:, :-1]
        grown[:, :-1] |= outside[:, 1:]
        grown &= open_
        if (grown == outside).all():
            break
        outside = grown
    return ~outside[1:-1, 1:-1]


# ── Диск ────────────────────────────────────────────────────────────────────


def _fill_polygon(grid: np.ndarray, poly: list[tuple[float, float]], origin: float) -> None:
    """Заповнити багатокутник (мм від центру) на сітці — парність перетинів."""
    if len(poly) < 3:
        return
    xs = np.array([p[0] for p in poly])
    ys = np.array([p[1] for p in poly])
    x2, y2 = np.roll(xs, -1), np.roll(ys, -1)
    n = grid.shape[0]
    lo = max(0, int((ys.min() - origin) / GRID_MM))
    hi = min(n - 1, int((ys.max() - origin) / GRID_MM) + 1)
    for row in range(lo, hi + 1):
        y = origin + (row + 0.5) * GRID_MM
        crosses = (ys <= y) != (y2 <= y)
        if not crosses.any():
            continue
        t = (y - ys[crosses]) / (y2[crosses] - ys[crosses])
        xc = np.sort(xs[crosses] + t * (x2[crosses] - xs[crosses]))
        for left, right in zip(xc[0::2], xc[1::2]):
            c0 = max(0, int(math.floor((left - origin) / GRID_MM)))
            c1 = min(n - 1, int(math.ceil((right - origin) / GRID_MM)))
            grid[row, c0: c1 + 1] = True


def _erode(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """Стиснути зайняте на `radius_cells` клітинок з кожного боку."""
    if radius_cells <= 0:
        return mask
    r = radius_cells
    return ~_dilate(~mask, r)[r:-r, r:-r]


def _islands(material: np.ndarray, attached: np.ndarray) -> np.ndarray:
    """Клітинки матеріалу, до яких від `attached` не дійти по матеріалу
    (4-сусідство) — повністю оточені дірками."""
    reach = attached & material
    while True:
        grown = reach.copy()
        grown[1:, :] |= reach[:-1, :]
        grown[:-1, :] |= reach[1:, :]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= material
        if (grown == reach).all():
            return material & ~reach
        reach = grown


def _grid(radius: float, contours: list[list[tuple[float, float]]]):
    """(дірки з острівцями, коло диска, відстань від центру) на сітці.

    Острівець — матеріал, повністю оточений вирізаними дірками: у програмі він
    «вільний», а в диску його вже нема — випадає разом із сусідніми роботами
    (власник 18.11.26: «рахуєш вільне місце, яке є острівцями»). Тонкі
    перемички тримаються — втраченим вважаємо лише те, що з краєм диска не
    з'єднане зовсім. Край диска — кільце тримача за колом `radius`."""
    n = int(math.ceil(2 * radius / GRID_MM))
    origin = -radius
    centres = origin + (np.arange(n) + 0.5) * GRID_MM
    yy, xx = np.meshgrid(centres, centres, indexing="ij")
    dist = np.hypot(xx, yy)
    disc = dist <= radius
    holes = np.zeros((n, n), dtype=bool)
    for poly in contours:
        _fill_polygon(holes, poly, origin)
    material = disc & ~holes
    edge = disc & (dist > radius - GRID_MM)
    return holes | _islands(material, edge), disc, dist


def disc_blocked(radius: float, contours: list[list[tuple[float, float]]]) -> np.ndarray:
    """Зайняті клітинки диска: поза колом (з краєм), під вирізаними дірками й
    острівцями — стиснутими на `HOLE_OVERLAP_MM`, бо запас нової роботи може
    трохи на них заходити."""
    holes, _disc, dist = _grid(radius, contours)
    holes = _erode(holes, int(round(HOLE_OVERLAP_MM / GRID_MM)))
    return (dist > radius - RIM_MM) | holes


def usable_share(radius: float, contours: list[list[tuple[float, float]]]) -> float:
    """Частка диска (0..1), що лишилась матеріалом: без дірок і без острівців.
    Сума площ обрисів рахувала острівці вільними."""
    holes, disc, _dist = _grid(radius, contours)
    total = int(disc.sum())
    return float((disc & ~holes).sum()) / total if total else 0.0


# ── Розстановка ─────────────────────────────────────────────────────────────


def _place(blocked: np.ndarray, mask: np.ndarray) -> Optional[tuple[int, int]]:
    """Місце (рядок, стовпець лівого верхнього кута) без перетину, ближче до
    краю диска; None — немає."""
    H, W = blocked.shape
    h, w = mask.shape
    if h > H or w > W:
        return None
    shape = (H + h - 1, W + w - 1)
    conv = np.fft.irfft2(
        np.fft.rfft2(blocked.astype(np.float32), shape) * np.fft.rfft2(mask[::-1, ::-1].astype(np.float32), shape),
        shape,
    )
    hits = conv[h - 1: H, w - 1: W]  # позиції, де маска цілком на сітці
    free = hits < 0.5
    if not free.any():
        return None
    rows, cols = np.nonzero(free)
    cy = rows + h / 2 - H / 2
    cx = cols + w / 2 - W / 2
    best = int(np.argmax(cy * cy + cx * cx))
    return int(rows[best]), int(cols[best])


def place_all(blocked: np.ndarray, pieces: list[np.ndarray], scale: float = 1.0) -> bool:
    """Поставити всі конструкції по черзі (найбільша перша). `blocked`
    змінюється — передавати копію."""
    order = sorted(pieces, key=lambda p: float(np.ptp(p[:, 0]) * np.ptp(p[:, 1])), reverse=True)
    for points in order:
        placed = False
        for angle in ANGLES:
            mask = piece_mask(points, angle, scale)
            spot = _place(blocked, mask)
            if spot is None:
                continue
            r, c = spot
            blocked[r: r + mask.shape[0], c: c + mask.shape[1]] |= mask
            placed = True
            break
        if not placed:
            return False
    return True


def wider_than_disc(points: np.ndarray, radius: float, scale: float = 1.0) -> bool:
    """Конструкція ширша за диск за будь-якого повороту: розмах по осі не більший
    за найбільшу відстань між точками, а та мусить влізти в діаметр.

    Без цієї перевірки STL із далекою зайвою вершиною чи другим тілом за сотні мм
    будував сітку в мільйони клітинок, і `_fill_holes` ішов годинами: 17.11.26
    запит підказки листа 3709 висів 3 год 9 хв, а при встановленні 0.21.147
    тримав старий процес — інсталятор відкотився."""
    if points is None or not len(points):
        return False
    return float(np.ptp(points, axis=0).max()) * scale > 2 * radius


def fits(radius: float, contours: list[list[tuple[float, float]]], pieces: list[np.ndarray],
         scale: float = 1.0) -> bool:
    """Чи стануть усі конструкції на цей диск поруч із уже розставленим."""
    if not pieces:
        return True
    if any(wider_than_disc(p, radius, scale) for p in pieces):
        return False
    return place_all(disc_blocked(radius, contours), pieces, scale)
