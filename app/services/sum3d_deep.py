"""Де Sum3D тримає коефіцієнт усадки — розвідка поза файлами дисків (лише читання).

`sum3d_files` показав (04.11.26, прод 0.21.92–0.21.94): у `.blk` коефіцієнта
немає — ні біля маркерів, ні серед круглих чисел, ні на тому самому місці, де
в ПММА 1.0. Власнику збирати пари «наряд → товщина» незручно, тому шукаємо
далі самі, на тому самому ПК, де стоїть Sum3D:

* **Налаштування** (`scan_settings`) — теки Sum3D і реєстр: рядки зі словами
  shrink/scale/factor і назвами брендів.
* **Проєкт `.cam`** (`probe_cam`) — матриця 4×4 з рівномірним масштабом (норми
  стовпців однакові, тож поворот не заважає), три однакові числа поспіль
  (вектор масштабу), задане число точно з оточенням і вершини STL: як є чи
  помножені.
* **Звірка груп** (`compare_groups`) — місце, де в усіх проєктах ПММА рівно
  1.0, а в усіх цирконієвих 1.1–1.4 або обернене.

Межі ті самі, що в `sum3d_files`: нічого не пишемо; назв STL і текстових
значень не віддаємо. Рядок налаштувань віддаємо лише якщо є маркер чи бренд, без
кирилиці й без рядків зі шляхами (там могла б бути назва STL з іменем пацієнта).
"""

from __future__ import annotations

import math
import os
import re
import struct
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import numpy as np

from app.services.sum3d_files import (
    KEYWORDS,
    MAX_READ_BYTES,
    PLAUSIBLE_HIGH,
    PLAUSIBLE_LOW,
    _as_text,
    _numbers_after,
    _WORD_PATTERNS,
)

BRANDS = re.compile(r"monolith|emotion|katana|z-?nature|doceram|stml|zircon|cercon", re.IGNORECASE)
_SUM3D_DIR = re.compile(r"sum3d|cimsystem", re.IGNORECASE)

# Великого й чужого не читаємо: диски, проєкти, сітки, програми, бібліотеки.
SKIP_EXT = frozenset({
    ".stl", ".blk", ".cam", ".iso", ".nc", ".exe", ".dll", ".ocx", ".sys", ".msi", ".cab",
    ".png", ".jpg", ".jpeg", ".bmp", ".ico", ".gif", ".tif", ".tiff", ".svg", ".pdf", ".chm",
    ".zip", ".rar", ".7z", ".tmp", ".qm", ".ttf", ".otf", ".pak", ".wav", ".mp3", ".mp4", ".avi",
    ".lib", ".pdb", ".obj", ".lnk", ".ply", ".dxf", ".step", ".stp", ".igs", ".iges",
})
SKIP_DIRS = frozenset({"cam-blanks", "cam-work", "export", "backup", "backups"})
MAX_SETTINGS_FILE = 4 * 1024 * 1024
MAX_FILES_READ = 4000
SCAN_DEADLINE_SECONDS = 45.0
MAX_FINDINGS = 60
MAX_LINES_PER_FILE = 30
MAX_LINE_CHARS = 160

_SAFE_PART = re.compile(r"^[A-Za-z0-9_.\- ]{1,40}$")
_LINE_TOKEN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.\-]*|[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?|[=:;,<>\"'\[\]{}()]"
)


# ── Налаштування ────────────────────────────────────────────────────────────


def settings_roots(blanks_root: str) -> list[str]:
    """Теки Sum3D: батьківська до дисків (`C:\\sum` для `C:\\sum\\DENTAL\\CAM-blanks`,
    але не корінь диска) і теки з його іменем у стандартних місцях Windows."""
    roots: list[str] = []
    if blanks_root:
        base = Path(blanks_root).parent
        if base.parent != base.parent.parent:
            base = base.parent
        roots.append(str(base))
    for variable in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        place = os.environ.get(variable)
        if not place:
            continue
        try:
            with os.scandir(place) as entries:
                roots += [e.path for e in entries if _SUM3D_DIR.search(e.name) and e.is_dir()]
        except OSError:
            continue
    return roots


def scan_settings(roots: Iterable[str]) -> dict[str, Any]:
    """Рядки й двійкові маркери зі словами усадки чи брендів у файлах налаштувань."""
    deadline = time.monotonic() + SCAN_DEADLINE_SECONDS
    by_ext: Counter[str] = Counter()
    findings: list[dict[str, Any]] = []
    total = read = 0
    cut = False
    for root in roots:
        for path, size in _walk(root):
            total += 1
            suffix = Path(path).suffix.lower()
            by_ext[suffix or "(без розширення)"] += 1
            if suffix in SKIP_EXT or size > MAX_SETTINGS_FILE or size == 0:
                continue
            if read >= MAX_FILES_READ or time.monotonic() > deadline:
                cut = True
                continue
            read += 1
            found = _file_findings(path)
            if found and len(findings) < MAX_FINDINGS:
                findings.append({"файл": _mask_path(path, root), "розмір": size, **found})
    return {
        "корені": [str(r) for r in roots],
        "файлів": total,
        "прочитано": read,
        "обрізано": cut,
        "за_розширенням": dict(by_ext.most_common(30)),
        "знахідки": findings,
    }


def _walk(root: str) -> Iterator[tuple[str, int]]:
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in SKIP_DIRS:
                                pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield entry.path, entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue


def _mask_path(path: str, root: str) -> str:
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        parts = Path(path).parts[-1:]
    shown = [p if _SAFE_PART.match(p) else "<…>" for p in parts]
    return "/".join([Path(root).name, *shown])


def _file_findings(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_SETTINGS_FILE)
    except OSError:
        return {}
    text = _as_text(data)
    if text is not None:
        lines = marked_lines(text)
        return {"рядки": lines} if lines else {}
    markers = _binary_markers(data)
    return {"маркери": markers} if markers else {}


def marked_lines(text: str) -> list[str]:
    """Рядки з маркером чи брендом — лише латинські ідентифікатори й числа.

    Рядок зі шляхом чи назвою STL відкидається цілком: у списку недавніх
    файлів могла б бути назва з іменем пацієнта."""
    out: dict[str, None] = {}
    for raw in text.splitlines():
        if not (KEYWORDS.search(raw) or BRANDS.search(raw)):
            continue
        if "\\" in raw or "/" in raw or ".stl" in raw.lower():
            continue
        clean = " ".join(_LINE_TOKEN.findall(raw))[:MAX_LINE_CHARS]
        if clean:
            out.setdefault(clean)
        if len(out) >= MAX_LINES_PER_FILE:
            break
    return list(out)


def _binary_markers(data: bytes) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for pattern, encoding in _WORD_PATTERNS:
        for match in pattern.finditer(data):
            word = match.group().decode(encoding, "replace")
            if not (KEYWORDS.search(word) or BRANDS.search(word)):
                continue
            markers.append({
                "маркер": word,
                "кодування": encoding,
                "зсув": match.start(),
                "кандидати": _numbers_after(data, match.end(), big_endian=encoding == "utf-16-be"),
            })
            if len(markers) >= MAX_LINES_PER_FILE:
                return markers
    return markers


def scan_registry() -> dict[str, Any]:
    """Гілки `Software\\<Sum3D|CIMsystem>` у реєстрі (Qt так зберігає налаштування).

    Лише числа: значення, чия назва чи гілка містить маркер або бренд, і будь-яке
    значення 1.10–1.40. Рядки не віддаємо."""
    hits = [
        {"гілка": key, "назва": name, "значення": number}
        for key, name, value in _registry_values()
        if (number := _registry_number(value)) is not None
        and (
            KEYWORDS.search(name) or BRANDS.search(name)
            or KEYWORDS.search(key) or BRANDS.search(key)
            or PLAUSIBLE_LOW <= number <= PLAUSIBLE_HIGH
        )
    ]
    return {"значень": hits[:100], "усього": len(hits)}


def _registry_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _registry_values() -> Iterator[tuple[str, str, Any]]:
    try:
        import winreg
    except ImportError:
        return
    for hive, hive_name in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for vendor in _registry_subkeys(winreg, hive, "Software"):
            if _SUM3D_DIR.search(vendor):
                yield from _registry_walk(winreg, hive, f"Software\\{vendor}", hive_name, depth=0)


def _registry_subkeys(winreg: Any, hive: Any, path: str) -> list[str]:
    names: list[str] = []
    try:
        with winreg.OpenKey(hive, path) as key:
            for index in range(winreg.QueryInfoKey(key)[0]):
                names.append(winreg.EnumKey(key, index))
    except OSError:
        pass
    return names


def _registry_walk(
    winreg: Any, hive: Any, path: str, hive_name: str, *, depth: int
) -> Iterator[tuple[str, str, Any]]:
    try:
        with winreg.OpenKey(hive, path) as key:
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, _kind = winreg.EnumValue(key, index)
                yield f"{hive_name}\\{path}", name, value
    except OSError:
        return
    if depth < 6:
        for child in _registry_subkeys(winreg, hive, path):
            yield from _registry_walk(winreg, hive, f"{path}\\{child}", hive_name, depth=depth + 1)


# ── Проєкт .cam ─────────────────────────────────────────────────────────────

_NUMBER_FORMATS = (("double LE", "<f8", "<d"), ("double BE", ">f8", ">d"), ("float LE", "<f4", "<f"), ("float BE", ">f4", ">f"))
MAX_ONE_HITS = 300_000
SCALE_LOW = 0.5
SCALE_HIGH = 2.0
TRIPLE_RANGES = ((1.05, 1.45), (0.69, 0.95))
MAX_GROUPS = 30
VERTEX_SAMPLE = 60


def probe_cam(
    path: str, *, stl_paths: Iterable[str] = (), expected: Optional[float] = None
) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_READ_BYTES)
    except OSError as exc:
        return {"помилка": f"не прочитано: {exc.__class__.__name__}"}
    out: dict[str, Any] = {
        "розмір": len(data),
        "матриці_4х4": affine_scales(data),
        "три_однакові_поспіль": equal_triples(data),
    }
    if expected is not None:
        out["точне_значення"] = exact_value(data, expected)
    vertices = sample_vertices(stl_paths)
    if vertices:
        out["вершини_stl"] = vertex_presence(data, vertices, expected)
    return out


def affine_scales(data: bytes) -> list[dict[str, Any]]:
    """Матриці 4×4: кінець рядка/стовпця `0 0 0 1`, далі норми трьох рядків
    верхнього 3×3. Поворот норми не змінює, тож рівномірний масштаб видно як
    три однакові числа — і саме тому тут не треба шукати кругле 1.25."""
    groups: dict[tuple[str, tuple[float, float, float]], dict[str, Any]] = {}
    for label, _dtype, code in _NUMBER_FORMATS:
        width = struct.calcsize(code)
        one = struct.pack(code, 1.0)
        fmt = f"{code[0]}16{code[1]}"
        start = 0
        hits = 0
        while hits < MAX_ONE_HITS:
            position = data.find(one, start)
            if position < 0:
                break
            start = position + 1
            hits += 1
            first = position - 15 * width
            if first < 0:
                continue
            values = struct.unpack_from(fmt, data, first)
            bottom_row = values[12] == values[13] == values[14] == 0.0
            last_column = values[3] == values[7] == values[11] == 0.0
            if not (bottom_row or last_column):
                continue
            if not all(math.isfinite(v) for v in values):
                continue
            norms = tuple(
                round(math.sqrt(sum(v * v for v in values[i:i + 3])), 5) for i in (0, 4, 8)
            )
            if not all(SCALE_LOW <= n <= SCALE_HIGH for n in norms):
                continue
            key = (label, (norms[0], norms[1], norms[2]))
            group = groups.setdefault(
                key, {"тип": label, "масштаб": list(norms), "разів": 0, "перший_зсув": first}
            )
            group["разів"] += 1
    found = sorted(groups.values(), key=lambda g: -g["разів"])
    return found[:MAX_GROUPS]


def equal_triples(data: bytes) -> list[dict[str, Any]]:
    """Три однакові числа поспіль у межах коефіцієнта — вектор масштабу (k, k, k)."""
    groups: dict[tuple[str, float], dict[str, Any]] = {}
    for label, dtype, _code in _NUMBER_FORMATS:
        width = np.dtype(dtype).itemsize
        for shift in range(width):
            count = (len(data) - shift) // width
            if count < 3:
                continue
            with np.errstate(invalid="ignore", over="ignore"):
                values = np.frombuffer(data, dtype=dtype, count=count, offset=shift).astype(np.float64)
            same = (values[:-2] == values[1:-1]) & (values[1:-1] == values[2:])
            for low, high in TRIPLE_RANGES:
                for index in np.nonzero(same & (values[:-2] >= low) & (values[:-2] <= high))[0]:
                    value = round(float(values[index]), 6)
                    group = groups.setdefault(
                        (label, value),
                        {"тип": label, "значення": value, "разів": 0,
                         "перший_зсув": shift + int(index) * width},
                    )
                    group["разів"] += 1
    return sorted(groups.values(), key=lambda g: -g["разів"])[:MAX_GROUPS]


def exact_value(data: bytes, value: float) -> dict[str, Any]:
    """Де лежить задане число точно, з оточенням — лише числа."""
    out: dict[str, Any] = {}
    for label, _dtype, code in _NUMBER_FORMATS:
        needle = struct.pack(code, value)
        width = len(needle)
        offsets: list[int] = []
        start = 0
        count = 0
        while True:
            position = data.find(needle, start)
            if position < 0:
                break
            count += 1
            if len(offsets) < 5:
                offsets.append(position)
            start = position + 1
        samples = []
        for position in offsets[:3]:
            before = max(0, (position - 8 * width))
            around = struct.unpack_from(
                f"{code[0]}{(position - before) // width + 9}{code[1]}", data, before
            ) if position + 9 * width <= len(data) else ()
            samples.append({
                "зсув": position,
                "оточення": [round(v, 5) if math.isfinite(v) else None for v in around],
            })
        out[label] = {"разів": count, "приклади": samples}
    return out


def sample_vertices(stl_paths: Iterable[str]) -> list[tuple[float, float, float]]:
    """Рівномірна вибірка вершин двійкових STL (лише координати, без назв)."""
    vertices: list[tuple[float, float, float]] = []
    for path in stl_paths:
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        if len(data) < 84:
            continue
        count = struct.unpack_from("<I", data, 80)[0]
        if not count or 84 + count * 50 != len(data):
            continue
        step = max(1, count // VERTEX_SAMPLE)
        for index in range(0, count, step):
            vertices.append(struct.unpack_from("<3f", data, 84 + index * 50 + 12))
    return vertices[:VERTEX_SAMPLE]


def vertex_presence(
    data: bytes, vertices: list[tuple[float, float, float]], expected: Optional[float]
) -> dict[str, Any]:
    """Скільки вершин STL лежить у проєкті як є й помножених на `expected`:
    як є — сітка без масштабу (тоді він окремо); помножені — проєкт зберігає
    вже збільшену сітку."""
    out: dict[str, Any] = {
        "перевірено": len(vertices),
        "як_є_float": sum(struct.pack("<3f", *v) in data for v in vertices),
        "як_є_double": sum(struct.pack("<3d", *v) in data for v in vertices),
    }
    if expected is not None:
        scaled = [tuple(c * expected for c in v) for v in vertices]
        out["помножені_float"] = sum(struct.pack("<3f", *v) in data for v in scaled)
        out["помножені_double"] = sum(struct.pack("<3d", *v) in data for v in scaled)
    return out


# ── Звірка ПММА проти цирконію ──────────────────────────────────────────────

COMPARE_BYTES = 256 * 1024


def compare_groups(
    plain: list[tuple[str, str]], shrunk: list[tuple[str, str]], *, nbytes: int = COMPARE_BYTES
) -> dict[str, Any]:
    """Зсуви, де в УСІХ файлах без усадки рівно 1.0, а в УСІХ з усадкою
    коефіцієнт (1.10–1.40) або обернений (0.71–0.91)."""
    plain_heads = _read_heads(plain, nbytes)
    shrunk_heads = _read_heads(shrunk, nbytes)
    if not plain_heads or not shrunk_heads:
        return {"примітка": "потрібен хоча б один файл у кожній групі",
                "без_усадки": len(plain_heads), "з_усадкою": len(shrunk_heads)}
    shortest = min(len(d) for _n, d in plain_heads + shrunk_heads)
    rows: list[dict[str, Any]] = []
    for label, dtype, _code in _NUMBER_FORMATS:
        width = np.dtype(dtype).itemsize
        for shift in range(width):
            count = (shortest - shift) // width
            if count <= 0:
                continue
            mask = np.ones(count, dtype=bool)
            shrunk_values = []
            with np.errstate(invalid="ignore", over="ignore"):
                for _name, data in plain_heads:
                    mask &= np.frombuffer(data, dtype=dtype, count=count, offset=shift) == 1.0
                for _name, data in shrunk_heads:
                    values = np.frombuffer(data, dtype=dtype, count=count, offset=shift).astype(np.float64)
                    shrunk_values.append(values)
                    mask &= ((values >= PLAUSIBLE_LOW) & (values <= PLAUSIBLE_HIGH)) | (
                        (values >= 0.71) & (values <= 0.91)
                    )
            for index in np.nonzero(mask)[0]:
                rows.append({
                    "зсув": shift + int(index) * width,
                    "тип": label,
                    "значення": {
                        name: round(float(values[index]), 6)
                        for (name, _d), values in zip(shrunk_heads, shrunk_values)
                    },
                })
    rows.sort(key=lambda r: r["зсув"])
    return {
        "без_усадки": [n for n, _d in plain_heads],
        "з_усадкою": [n for n, _d in shrunk_heads],
        "байтів_звірено": shortest,
        "місця": rows[:40],
        "місць_усього": len(rows),
    }


def _read_heads(files: list[tuple[str, str]], nbytes: int) -> list[tuple[str, bytes]]:
    heads: list[tuple[str, bytes]] = []
    for name, path in files:
        try:
            with open(path, "rb") as handle:
                heads.append((name, handle.read(nbytes)))
        except OSError:
            continue
    return heads
