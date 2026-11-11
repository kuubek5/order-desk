"""Геометрія у файлі диска Sum3D `.blk` — розвідка формату (лише читання).

Навіщо. Табло цеху малює диск із лунками, але місця лунок вигадані (спіраль
у `shop_board._disc_layout`), а власник хоче справжні: де на диску вже
вирізано й де ріжеться зараз (11.11.26). `kmill_sum3d_files kind=blk` показав,
що диск перезаписується разом із проєктом, росте з ужитком, а в дисках 18 мм
число 9.0 стоїть сотні разів із кроком 24 байти — тобто трійки (x, y, z) на
середній площині диска. Сирого файлу в розробці немає (він лише на ПК цеху),
тож цей модуль — очі MCP: дістає з файлу ЧИСЛА так, щоб формат можна було
розібрати з одного виклику, а вже потім, окремим кроком і з тестом, читати
диск для табло.

Що віддає:

* **Ряди чисел.** Суцільні відрізки правдоподібних `double` (будь-який зсув,
  не лише кратний 8). Ряд, у якому кожне третє число стале, — полілінія на
  одній висоті: межі, замкненість, площа, периметр і проріджені точки.
* **Проміжки між рядами** — службові заголовки (лічильники, ознаки, можливо
  звʼязок із проєктом): шістнадцятковий запис початку й кінця та останні
  цілі числа перед рядом.

Межі ті самі, що в `sum3d_files`: нічого не пишемо, а текст назовні не йде.
У диску слів майже немає (марка, матеріал, заголовок), та запис проміжків —
це сирі байти, тому перед показом будь-який відрізок літер (ASCII, cp1251,
UTF-16) затирається знаком питання. Числа читаються з НЕзатертого файлу.
"""

from __future__ import annotations

import math
import re
from typing import Any

import numpy as np

from app.services.sum3d_files import MAX_READ_BYTES

# Правдоподібна координата чи розмір у мм. Нижня межа дуже мала свідомо:
# порахована геометрія дає «майже нулі» на кшталт 1.8e-16 (косинус прямого
# кута), і вищий поріг рвав контур на шматки саме на осях. Цілі числа,
# прочитані як double, — денормалізовані (~1e-314) і сюди не влучають;
# випадкові байти теж майже ні: показник мусить лягти у ~110 значень із 2048.
PLAUSIBLE_MIN = 1e-30
PLAUSIBLE_MAX = 1e4
# Коротший ряд — збіг, а не дані (дві точки з висотою = 6 чисел).
MIN_RUN_DOUBLES = 6
# Диск — десятки КБ. Більший файл цим інструментом не розбираємо: це не диск.
MAX_SHAPE_BYTES = 2 * 1024 * 1024
MAX_RUNS = 150
MAX_POINTS = 32
RAW_EDGE = 6
GAP_HEAD = 64
GAP_TAIL = 48
# Замкнений контур: перша й остання точки ближче за це, мм.
CLOSED_MM = 0.05

# Відрізки літер, які затираються в записі проміжків. Пороги — компроміс:
# коротше дало б десятки хибних затирань у двійкових числах.
_TEXT_RUNS = (
    re.compile(rb"[A-Za-z]{5,}"),
    re.compile(rb"[\xc0-\xff\xa8\xb8]{5,}"),
    re.compile(rb"(?:[A-Za-z]\x00|[\x10-\x4f\x51]\x04){4,}"),
    re.compile(rb"(?:\x00[A-Za-z]|\x04[\x10-\x4f\x51]){4,}"),
)


def describe_shape(path: str) -> dict[str, Any]:
    """Геометрія одного файлу диска; помилка читання — словом, не винятком."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_READ_BYTES)
    except OSError as exc:
        return {"помилка": f"не прочитано: {exc.__class__.__name__}"}
    return shape_of(data)


def shape_of(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_SHAPE_BYTES:
        return {"помилка": f"файл {len(data)} байтів — завеликий для диска, не розбираю"}
    runs = _double_runs(data)
    masked, masked_spans = _mask_text(data)
    described = [_describe_run(data, offset, count) for offset, count in runs[:MAX_RUNS]]
    heights: dict[str, int] = {}
    points_total = 0
    for run in described:
        const = run.get("стала")
        if const:
            key = _num(const["значення"], 3)
            heights[str(key)] = heights.get(str(key), 0) + 1
            points_total += run["точок"]
    covered = sum(count * 8 for _, count in runs)
    out: dict[str, Any] = {
        "байтів": len(data),
        "рядів_чисел": len(runs),
        "байтів_у_рядах_%": round(100 * covered / len(data), 1) if data else 0,
        "поліліній_за_висотою": heights,
        "точок_у_полілініях": points_total,
        "затерто_відрізків_тексту": masked_spans,
        "ряди": described,
        "проміжки": _gaps(masked, runs[:MAX_RUNS], len(data)),
    }
    if len(runs) > MAX_RUNS:
        out["обрізано"] = f"показано перші {MAX_RUNS} рядів із {len(runs)}"
    return out


def _double_runs(data: bytes) -> list[tuple[int, int]]:
    """Відрізки правдоподібних `double LE`: (зсув у файлі, скільки чисел).

    Зсув довільний, тому перебираються всі вісім фаз. З рядів, що
    перекриваються, лишається довший: справжні дані на своїй фазі дають довгий
    ряд, а на чужій — уривки.
    """
    found: list[tuple[int, int]] = []
    for phase in range(8):
        count = (len(data) - phase) // 8
        if count < MIN_RUN_DOUBLES:
            continue
        values = np.frombuffer(data, dtype="<f8", count=count, offset=phase)
        with np.errstate(invalid="ignore"):
            magnitude = np.abs(values)
            ok = np.isfinite(values) & (
                (values == 0) | ((magnitude >= PLAUSIBLE_MIN) & (magnitude <= PLAUSIBLE_MAX))
            )
        edges = np.flatnonzero(np.diff(np.concatenate(([0], ok.astype(np.int8), [0]))))
        for start, stop in zip(edges[::2].tolist(), edges[1::2].tolist()):
            if stop - start < MIN_RUN_DOUBLES:
                continue
            # Суцільні нулі — заповнювач, а не геометрія.
            if not np.any(values[start:stop]):
                continue
            found.append((phase + 8 * start, stop - start))
    found.sort(key=lambda run: (-run[1], run[0]))
    taken: list[tuple[int, int]] = []
    for offset, count in found:
        end = offset + 8 * count
        if any(offset < other_end and other < end for other, other_end in taken):
            continue
        taken.append((offset, end))
    return sorted((offset, (end - offset) // 8) for offset, end in taken)


def _describe_run(data: bytes, offset: int, count: int) -> dict[str, Any]:
    values = np.frombuffer(data, dtype="<f8", count=count, offset=offset)
    out: dict[str, Any] = {
        "зсув": offset,
        "чисел": count,
        "початок": [_num(v, 4) for v in values[:RAW_EDGE].tolist()],
        "кінець": [_num(v, 4) for v in values[-RAW_EDGE:].tolist()],
    }
    const = _const_phase(values)
    if const is None:
        # Не полілінія на одній висоті: межі по фазах кроку 3 і 2 — підказка,
        # що це (матриця, пари x-y, розміри).
        out["межі_крок3"] = [_span(values[p::3]) for p in range(3)]
        out["межі_крок2"] = [_span(values[p::2]) for p in range(2)]
        return out
    phase, height = const
    # Стала — третя в трійці, отже x стоїть одразу після неї.
    first = (phase + 1) % 3
    usable = (count - first) // 3
    xyz = values[first:first + usable * 3].reshape(usable, 3)
    xs, ys = xyz[:, 0], xyz[:, 1]
    closed = usable >= 3 and math.hypot(xs[0] - xs[-1], ys[0] - ys[-1]) <= CLOSED_MM
    steps = np.hypot(np.diff(xs), np.diff(ys))
    area = 0.5 * abs(float(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))))
    out.update({
        "стала": {"фаза": phase, "значення": _num(height, 4)},
        "точок": usable,
        "зайвих_чисел_на_початку": first,
        "x": _span(xs),
        "y": _span(ys),
        "центр": [_num(float(xs.mean()), 2), _num(float(ys.mean()), 2)],
        "замкнений": bool(closed),
        "периметр_мм": _num(float(steps.sum()), 2),
        "площа_мм2": _num(area, 2),
        "точки": _thin(xs, ys),
    })
    return out


def _const_phase(values: np.ndarray) -> tuple[int, float] | None:
    """Фаза кроку 3, на якій число стале (висота полілінії), або None."""
    for phase in range(3):
        column = values[phase::3]
        if len(column) >= 2 and float(np.ptp(column)) <= 1e-9:
            # Дві інші фази мусять мінятись, інакше це три сталі, а не лінія.
            others = [values[p::3] for p in range(3) if p != phase]
            if any(len(o) >= 2 and float(np.ptp(o)) > 1e-6 for o in others):
                return phase, float(column[0])
    return None


def _thin(xs: np.ndarray, ys: np.ndarray) -> list[list[float]]:
    """Не більше `MAX_POINTS` точок, рівномірно; перша й остання — завжди."""
    count = len(xs)
    index: list[int]
    if count <= MAX_POINTS:
        index = list(range(count))
    else:
        index = sorted({round(i * (count - 1) / (MAX_POINTS - 1)) for i in range(MAX_POINTS)})
    return [[_num(float(xs[i]), 2), _num(float(ys[i]), 2)] for i in index]


def _span(column: np.ndarray) -> list[float]:
    if not len(column):
        return []
    return [_num(float(column.min()), 3), _num(float(column.max()), 3)]


def _num(value: float, digits: int) -> float:
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _mask_text(data: bytes) -> tuple[bytes, int]:
    """Копія файлу, де відрізки літер затерто; і скільки таких відрізків."""
    buffer = bytearray(data)
    spans = 0
    for pattern in _TEXT_RUNS:
        for match in pattern.finditer(data):
            buffer[match.start():match.end()] = b"?" * (match.end() - match.start())
            spans += 1
    return bytes(buffer), spans


def _gaps(masked: bytes, runs: list[tuple[int, int]], size: int) -> list[dict[str, Any]]:
    """Байти між рядами: службові заголовки формату."""
    out: list[dict[str, Any]] = []
    cursor = 0
    for offset, count in [*runs, (size, 0)]:
        if offset > cursor:
            out.append(_describe_gap(masked, cursor, offset))
        cursor = max(cursor, offset + 8 * count)
    return out


def _describe_gap(masked: bytes, start: int, end: int) -> dict[str, Any]:
    chunk = masked[start:end]
    out: dict[str, Any] = {"зсув": start, "довжина": len(chunk)}
    if len(chunk) <= GAP_HEAD + GAP_TAIL:
        out["hex"] = chunk.hex(" ")
    else:
        out["hex_початок"] = chunk[:GAP_HEAD].hex(" ")
        out["hex_кінець"] = chunk[-GAP_TAIL:].hex(" ")
    # Лічильник точок найчастіше стоїть цілим числом одразу перед рядом.
    tail = chunk[-16:]
    usable = len(tail) - len(tail) % 4
    if usable:
        out["int32_перед_рядом"] = np.frombuffer(
            tail[len(tail) - usable:], dtype="<i4"
        ).tolist()
    return out
