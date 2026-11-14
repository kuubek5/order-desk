"""Чий обрис на диску: сліди обрисів `.blk` у проєкті Sum3D (власник 14.11.26 —
підсвітити на табло криву, що фрезерується зараз).

Файл диска зберігає лише обриси (`sum3d_disc`), без позначки, якого вони
проєкту. Тут — РОЗВІДКА, не правило. Шукаємо два незалежні сліди того, що
обрис належить проєкту програми:

1. точки обрису лежать у проєкті тими самими байтами — пара x, y поспіль
   (double або float; трійка x, y, z містить ту саму пару);
2. зсув матриці 4×4 з проєкту (розстановка моделі) падає всередину обрису.

Котрий слід Sum3D справді пише, і чи лише СВОЇ обриси, чи весь диск, —
звіряється на проді (`kmill_sum3d_files kind=disc_link`). На табло звідси
нічого не йде: хибна підсвітка гірша за жодну.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from app.services.sum3d_disc import BlankShape

POINTS_PER_CONTOUR = 6
MAX_OFFSETS = 400
_NORM_LOW, _NORM_HIGH = 0.5, 2.0


def sample_points(contour: list[tuple[float, float]], count: int = POINTS_PER_CONTOUR) -> list[tuple[float, float]]:
    """Рівномірно розкидані точки обрису без «круглих» пар: (0.0, 12.5)
    трапляється в проєкті й без жодного зв'язку з обрисом."""
    points = [p for p in contour if not (_round(p[0]) and _round(p[1]))]
    if not points:
        return []
    step = max(1, len(points) // count)
    return points[::step][:count]


def _round(value: float) -> bool:
    return abs(value * 100 - round(value * 100)) < 1e-6


def _views(data: bytes, dtype: str) -> list[np.ndarray]:
    width = np.dtype(dtype).itemsize
    out = []
    for shift in range(width):
        count = (len(data) - shift) // width
        if count >= 2:
            out.append(np.frombuffer(data, dtype=dtype, count=count, offset=shift))
    return out


def point_hits(data: bytes, contours: list[list[tuple[float, float]]], dtype: str = "<f8") -> list[int]:
    """Скільки вибраних точок кожного обрису лежить у `data` парою x, y поспіль."""
    samples = [sample_points(c) for c in contours]
    cast = np.float32 if dtype == "<f4" else np.float64
    wanted: dict[float, list[tuple[int, float]]] = {}
    for index, points in enumerate(samples):
        for x, y in points:
            wanted.setdefault(float(cast(x)), []).append((index, float(cast(y))))
    hits: list[set[tuple[float, float]]] = [set() for _ in contours]
    if not wanted:
        return [0] * len(contours)
    keys = np.array(sorted(wanted), dtype=np.float64)
    for view in _views(data, dtype):
        with np.errstate(invalid="ignore", over="ignore"):
            values = view.astype(np.float64)
            pos = np.clip(np.searchsorted(keys, values), 0, len(keys) - 1)
            found = np.nonzero(keys[pos] == values)[0]
        for i in found:
            if i + 1 >= len(values):
                continue
            x = float(values[i])
            y = float(values[i + 1])
            for index, want_y in wanted.get(x, ()):
                if want_y == y:
                    hits[index].add((x, y))
    return [len(h) for h in hits]


def matrix_offsets(data: bytes) -> list[tuple[float, float, float]]:
    """Зсуви матриць 4×4 (double): нижній рядок `0 0 0 1` — зсув у стовпці 3,
    останній стовпець `0 0 0 1` — зсув у рядку 3. Норми рядків 3×3 у межах
    0.5–2 (поворот із масштабом), нульовий зсув не береться."""
    seen: set[tuple[float, float, float]] = set()
    for view in _views(data, "<f8"):
        with np.errstate(invalid="ignore", over="ignore"):
            ones = np.nonzero(view == 1.0)[0]
            ones = ones[ones >= 15]
            bottom = (view[ones - 1] == 0) & (view[ones - 2] == 0) & (view[ones - 3] == 0)
            column = (view[ones - 4] == 0) & (view[ones - 8] == 0) & (view[ones - 12] == 0)
            ones = ones[bottom | column]
        for i in ones:
            start = int(i) - 15
            if start < 0:
                continue
            m = [float(v) for v in view[start:start + 16]]
            if not all(math.isfinite(v) for v in m):
                continue
            if m[12] == m[13] == m[14] == 0.0:
                offset = (m[3], m[7], m[11])
                rows = (m[0:3], m[4:7], m[8:11])
            elif m[3] == m[7] == m[11] == 0.0:
                offset = (m[12], m[13], m[14])
                rows = (m[0:3], m[4:7], m[8:11])
            else:
                continue
            if not all(_NORM_LOW <= math.sqrt(sum(v * v for v in r)) <= _NORM_HIGH for r in rows):
                continue
            if offset == (0.0, 0.0, 0.0) or max(abs(v) for v in offset) > 500:
                continue
            seen.add((round(offset[0], 3), round(offset[1], 3), round(offset[2], 3)))
            if len(seen) >= MAX_OFFSETS:
                return sorted(seen)
    return sorted(seen)


def inside(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    result = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            result = not result
    return result


def link_report(data: bytes, shape: BlankShape) -> dict[str, Any]:
    """Сліди кожного обрису в проєкті: точки (double/float) і матриці всередині."""
    doubles = point_hits(data, shape.contours, "<f8")
    floats = point_hits(data, shape.contours, "<f4")
    offsets = matrix_offsets(data)
    in_contour = [0] * len(shape.contours)
    outside = 0
    for x, y, _z in offsets:
        owners = [i for i, c in enumerate(shape.contours) if inside(x, y, c)]
        for i in owners:
            in_contour[i] += 1
        if not owners and math.hypot(x, y) <= shape.radius:
            outside += 1
    rows = []
    for i, contour in enumerate(shape.contours):
        cx = sum(p[0] for p in contour) / len(contour)
        cy = sum(p[1] for p in contour) / len(contour)
        rows.append({
            "№": i + 1,
            "центр_мм": [round(cx, 1), round(cy, 1)],
            "площа_мм2": round(shape.areas[i]),
            "точок_перевірено": len(sample_points(contour)),
            "точок_double": doubles[i],
            "точок_float": floats[i],
            "матриць_усередині": in_contour[i],
        })
    traced = [r["№"] for r in rows if r["точок_double"] or r["точок_float"] or r["матриць_усередині"]]
    return {
        "обрисів": len(rows),
        "обриси_зі_слідом": traced,
        "матриць_знайдено": len(offsets),
        "матриць_на_диску_поза_обрисами": outside,
        "обриси": rows,
    }
