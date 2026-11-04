"""Що Sum3D пише у свої файли — для калібрування усадки (лише читання).

Навіщо. Бейдж товщини в черзі (`stl_height`) множить висоту STL на СЕРЕДНІЙ
коефіцієнт родини (цирконій ×1.2), а справжній свій у кожного диска: на
Monolith 37896 Sum3D множив на 1.25, і бейдж розходився з Sum3D на десяті мм
(власник 03.11.26). Коефіцієнт з етикетки оператор вводить, коли створює диск
у CAM, тож він має лежати у файлі диска `.blk` або в проєкті `.cam`. Формату
цих файлів ми не бачили: файли на ПК цеху, а з дому туди доступу немає. Цей
модуль — очі MCP-інструмента `kmill_sum3d_files`, щоб спершу ПОДИВИТИСЬ
формат, а вже потім (окремим кроком, з тестом) читати коефіцієнт у CRM.

Межі, свідомо вузькі:

* **Лише читання.** Файл відкривається на читання, нічого не пишеться й не
  переноситься.
* **Ніяких імен пацієнтів.** У проєкті Sum3D лежать STL з іменами пацієнтів
  у назвах. Тому з вмісту назовні йдуть лише: назви КЛЮЧІВ (схема формату —
  теги XML, ключі INI/JSON, що схожі на ідентифікатор), ЧИСЛОВІ значення й
  розміри. Текстові значення не віддаються ніколи; назви вкладених файлів
  архіву — лише розширення, якщо назва не схожа на службову.
  Назви самих `.blk` (`25-Monolith-A3-x51`) і `.cam` (`2026-11-03_13-12-29`)
  приватного не несуть — їх віддаємо.
* **Бінарне читаємо біля маркерів, а суцільно — лише малий файл і лише
  «круглі» числа.** У бінарному файлі може бути сітка STL, а координати вершин
  у мм лягають і в діапазон коефіцієнтів (1.1–1.4) — суцільний скан великого
  файлу дав би тисячі хибних «кандидатів». Тому числа шукаються поруч зі
  словами-маркерами (shrink, scale, factor…), а суцільний скан іде лише по
  файлу чи потоку до `ROUND_SCAN_MAX_BYTES` і бере тільки числа, що мають
  кілька знаків після коми (`1.25`, `1.2512`): коефіцієнт з етикетки людина
  вводить руками, координата сітки такою не буває майже ніколи.

Перший прогін на проді (03.11.26, 0.21.91): `.blk` і `.cam` — бінарні, і
жодного слова-маркера ні в ASCII, ні в UTF-16LE. Тому друга версія ще й
розпаковує zlib/gzip-потоки всередині файлу, читає UTF-16BE (так пише Qt
`QDataStream`) і шукає числа в обох порядках байтів.
"""

from __future__ import annotations

import io
import math
import os
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

BLANK_EXT = ".blk"
PROJECT_EXT = ".cam"

# Скільки байтів файлу читати максимум. Проєкт із вкладеними сітками буває
# десятки МБ; мережеве читання 64 МБ — кілька секунд, і це разовий виклик.
MAX_READ_BYTES = 64 * 1024 * 1024
# Вкладений текстовий файл архіву, більший за це, розбирається як бінарний.
MAX_TEXT_BYTES = 4 * 1024 * 1024

# Слова, біля яких шукаємо коефіцієнт. Регістр не важить.
KEYWORDS = re.compile(
    r"shrink|scal|factor|sinter|enlarg|expan|magnif|zoom|thick|height|oversiz",
    re.IGNORECASE,
)
# Коефіцієнт спікання цирконію — 1.15…1.35; беремо з запасом.
PLAUSIBLE_LOW = 1.10
PLAUSIBLE_HIGH = 1.40

# Суцільний пошук «круглих» чисел — лише в такому малому файлі чи потоці
# (диск `.blk` — десятки КБ; проєкт `.cam` із сітками — ні).
ROUND_SCAN_MAX_BYTES = 1024 * 1024
# Той самий коефіцієнт Sum3D може тримати по-різному: як збільшення (1.25),
# як обернене (0.8 = 1/1.25) або у відсотках (20 % усадки чи 25 % збільшення).
ROUND_RANGES = (
    ("коефіцієнт_1_10_до_1_40", PLAUSIBLE_LOW, PLAUSIBLE_HIGH),
    ("обернений_0_71_до_0_91", 0.71, 0.91),
    ("відсотки_9_до_30", 9.0, 30.0),
)
# Скільки знаків після коми ще вважається «введеним руками».
ROUND_DECIMALS_DOUBLE = 4
ROUND_DECIMALS_FLOAT = 3
ROUND_GROUPS_MAX = 40

# Стиснуті потоки всередині бінарного файлу: zlib (78 01/5E/9C/DA) і gzip.
_STREAM_HEAD = re.compile(rb"\x78[\x01\x5e\x9c\xda]|\x1f\x8b\x08")
MAX_STREAMS = 40
MAX_STREAMS_DESCRIBED = 12
MAX_STREAM_OUT = 8 * 1024 * 1024
MAX_STREAMS_TOTAL_OUT = 64 * 1024 * 1024

_NUMBER = re.compile(r"^[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\-]{0,47}$")
# Службова назва вкладеного файлу (project.xml, blank.ini) — без пробілів і
# кирилиці; усе інше (імʼя пацієнта, номер наряду з прізвищем) ховаємо.
_SAFE_ENTRY = re.compile(r"^[A-Za-z0-9_.\-/]{1,40}$")

_XML_TAG = re.compile(r"<([A-Za-z_][\w.:\-]*)([^<>]*)>([^<]*)")
_XML_ATTR = re.compile(r"([A-Za-z_][\w.:\-]*)\s*=\s*\"([^\"]*)\"")
_KV_LINE = re.compile(
    # Кома в значенні — лише десяткова між цифрами (`1,247`); інакше це
    # роздільник JSON.
    r"^\s*\"?([A-Za-z_][\w .:\-]{0,47}?)\"?\s*[:=]\s*\"?((?:[^\"\r\n,;}{\]\[]|(?<=\d),(?=\d)){0,48})",
    re.MULTILINE,
)
_ASCII_WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{3,47}")
_UTF16_WORD = re.compile(rb"(?:[A-Za-z_]\x00)(?:[A-Za-z0-9_]\x00){3,47}")
_UTF16BE_WORD = re.compile(rb"(?:\x00[A-Za-z_])(?:\x00[A-Za-z0-9_]){3,47}")
_WORD_PATTERNS = ((_ASCII_WORD, "ascii"), (_UTF16_WORD, "utf-16-le"), (_UTF16BE_WORD, "utf-16-be"))
_ASCII_NUMBER = re.compile(rb"[+-]?\d+[.,]\d+")
_HEADER_TEXT = re.compile(rb"[\x20-\x7e]{4,40}")


def _as_number(text: str) -> Optional[float]:
    value = text.strip()
    if not value or not _NUMBER.match(value):
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _plausible(value: float) -> bool:
    return PLAUSIBLE_LOW <= value <= PLAUSIBLE_HIGH


# ── Знайти файли ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FoundFile:
    path: str
    name: str
    size: int
    mtime: float


def newest_blanks(root: str, *, brand: str = "", limit: int = 2) -> list[FoundFile]:
    """Найновіші `.blk` у `<корінь>/<матеріал>/<висота>/` (як `cam_blanks`).

    `brand` — підрядок назви диска (`monolith`, `emo`), без регістру; кілька
    через кому (`pmma,monolith`) — по `limit` найновіших КОЖНОГО, щоб звірити
    ПММА з цирконієм в одному виклику. Час — `mtime`: тут він лише
    впорядковує вибірку для огляду, не рахує нічого.
    `DirEntry.stat()` на Windows бере дані з самого переліку теки — тисячі
    файлів не дають тисяч мережевих запитів.
    """
    brands = [b.strip().lower() for b in brand.split(",") if b.strip()]
    if len(brands) > 1:
        everything = newest_blanks(root, limit=10**9)
        picked: list[FoundFile] = []
        for wanted_brand in brands:
            picked += [f for f in everything if wanted_brand in f.name.lower() and f not in picked][:limit]
        return picked
    base = Path(root)
    wanted = brands[0] if brands else ""
    found: list[FoundFile] = []
    for material in _subdirs(base):
        for height in _subdirs(Path(material)):
            for entry in _entries(Path(height)):
                name = entry.name
                if not name.lower().endswith(BLANK_EXT):
                    continue
                if wanted and wanted not in name.lower():
                    continue
                stat = _stat(entry)
                if stat is None:
                    continue
                found.append(FoundFile(entry.path, name, stat.st_size, stat.st_mtime))
    found.sort(key=lambda f: f.mtime, reverse=True)
    return found[:limit]


def newest_projects(root: str, *, sum3d_id: str = "", limit: int = 2) -> list[FoundFile]:
    """Найновіші проєкти `.cam` у Cam-work; `sum3d_id` (`13-12-29`) — фільтр
    за часом у назві (той самий хвіст, що оператор пише в таблицю)."""
    wanted = sum3d_id.strip()
    found: list[FoundFile] = []
    for entry in _entries(Path(root)):
        name = entry.name
        if not name.lower().endswith(PROJECT_EXT):
            continue
        if wanted and wanted not in name:
            continue
        stat = _stat(entry)
        if stat is None:
            continue
        found.append(FoundFile(entry.path, name, stat.st_size, stat.st_mtime))
    found.sort(key=lambda f: f.mtime, reverse=True)
    return found[:limit]


def _entries(folder: Path) -> list[os.DirEntry]:
    try:
        return [e for e in os.scandir(folder) if _is_file(e)]
    except OSError:
        return []


def _subdirs(folder: Path) -> list[str]:
    try:
        return [e.path for e in os.scandir(folder) if e.is_dir()]
    except OSError:
        return []


def _is_file(entry: os.DirEntry) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def _stat(entry: os.DirEntry) -> Optional[os.stat_result]:
    try:
        return entry.stat()
    except OSError:
        return None


# ── Розібрати вміст ─────────────────────────────────────────────────────────


def describe_file(path: str) -> dict[str, Any]:
    """Формат файлу й усе, що в ньому схоже на коефіцієнт, — без текстових
    значень (див. докстрінг модуля)."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        return {"помилка": f"не прочитано: {exc.__class__.__name__}"}
    truncated = len(data) > MAX_READ_BYTES
    data = data[:MAX_READ_BYTES]
    out: dict[str, Any] = {"заголовок_hex": data[:16].hex(" ")}
    if truncated:
        out["обрізано"] = f"прочитано перші {MAX_READ_BYTES // (1024 * 1024)} МБ"
    out.update(describe_bytes(data))
    # Магічний рядок бінарного файлу (`<SUM3D_BLANK V1.0>`, `TECN 2.0`) — версія
    # формату. Лише бінарного й лише короткий, оборваний керівним байтом: у
    # текстовому файлі з початку йшли б уже значення (`<Name>…`).
    header = _HEADER_TEXT.match(data)
    if (
        out.get("формат") == "бінарний"
        and header
        and header.end() < len(data)
        and data[header.end()] < 0x20
    ):
        out["заголовок_текст"] = header.group().decode("ascii")
    return out


def describe_bytes(data: bytes, *, depth: int = 0) -> dict[str, Any]:
    if data[:4] == b"PK\x03\x04" and depth == 0:
        return _describe_zip(data)
    text = _as_text(data)
    if text is not None:
        return _describe_text(text)
    return _describe_binary(data, depth=depth)


def _describe_zip(data: bytes) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        return {"формат": "zip (пошкоджений або обрізаний)", **_describe_binary(data)}
    entries: list[dict[str, Any]] = []
    for index, info in enumerate(archive.infolist()[:60]):
        item: dict[str, Any] = {
            "назва": _safe_entry_name(info.filename, index),
            "розмір": info.file_size,
        }
        if info.is_dir():
            entries.append(item)
            continue
        # Сітку STL не розбираємо: там лише координати, і саме вони дали б
        # хибні «коефіцієнти».
        if info.filename.lower().endswith(".stl"):
            entries.append(item)
            continue
        try:
            with archive.open(info) as handle:
                inner = handle.read(MAX_TEXT_BYTES + 1)
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
            item["помилка"] = "не розпаковано"
            entries.append(item)
            continue
        item.update(describe_bytes(inner[:MAX_TEXT_BYTES], depth=1))
        entries.append(item)
    return {"формат": "zip", "файлів_в_архіві": len(archive.infolist()), "вміст": entries}


def _safe_entry_name(name: str, index: int) -> str:
    if _SAFE_ENTRY.match(name) and not name.lower().endswith(".stl"):
        return name
    suffix = Path(name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,6}", suffix):
        suffix = ""
    return f"<файл {index + 1}>{suffix}"


def _as_text(data: bytes) -> Optional[str]:
    """Текст, якщо файл схожий на текст; інакше None."""
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        sample = text[:4096]
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample)
        if printable >= 0.95 * len(sample):
            return text
        return None
    return None


def _describe_text(text: str) -> dict[str, Any]:
    pairs: list[tuple[str, str]] = []
    keys: dict[str, None] = {}
    if text.lstrip().startswith("<"):
        fmt = "xml"
        for tag, attrs, body in _XML_TAG.findall(text):
            keys.setdefault(tag)
            for attr, value in _XML_ATTR.findall(attrs):
                keys.setdefault(f"{tag}@{attr}")
                pairs.append((f"{tag}@{attr}", value))
            pairs.append((tag, body))
    else:
        fmt = "текст (ключ = значення)"
        for key, value in _KV_LINE.findall(text):
            key = key.strip()
            if _IDENT.match(key.replace(" ", "_")):
                keys.setdefault(key)
                pairs.append((key, value))
    numbers: list[dict[str, Any]] = []
    for key, value in pairs:
        number = _as_number(value)
        if number is None:
            continue
        numbers.append({"ключ": key, "значення": number})
    marked = [n for n in numbers if KEYWORDS.search(n["ключ"])]
    plausible = [n for n in numbers if _plausible(n["значення"])]
    return {
        "формат": fmt,
        "ключі": [k for k in keys if _IDENT.match(k.replace(" ", "_").replace("@", "."))][:200],
        "числа_біля_маркерів": marked[:100],
        "числа_1_10_до_1_40": plausible[:100],
        "усі_числа": numbers[:300],
        "чисел_усього": len(numbers),
    }


def _describe_binary(data: bytes, *, depth: int = 0) -> dict[str, Any]:
    markers: list[dict[str, Any]] = []
    words = 0
    for pattern, encoding in _WORD_PATTERNS:
        for match in pattern.finditer(data):
            words += 1
            if len(markers) >= 60:
                continue
            word = match.group().decode(encoding, "replace")
            if not KEYWORDS.search(word):
                continue
            markers.append({
                "маркер": word,
                "кодування": encoding,
                "зсув": match.start(),
                "кандидати": _numbers_after(data, match.end(), big_endian=encoding == "utf-16-be"),
            })
    out: dict[str, Any] = {
        "формат": "бінарний",
        "розмір": len(data),
        # ~8 біт/байт — стиснуте чи зашифроване; нижче — сирі структури.
        "ентропія_біт_на_байт": _entropy(data),
        # Лише кількість: самі слова можуть бути назвами STL (див. `common_words`).
        "слів_латиницею": words,
        "маркери": markers,
    }
    if len(data) <= ROUND_SCAN_MAX_BYTES:
        out["круглі_числа"] = _round_numbers(data)
    else:
        out["круглі_числа"] = (
            f"файл більший за {ROUND_SCAN_MAX_BYTES // 1024} КБ — суцільно не шукаємо "
            "(сітки STL дали б шум); див. потоки"
        )
    if depth == 0:
        streams = _compressed_streams(data)
        if streams:
            out["стиснуті_потоки"] = [
                {
                    "зсув": offset,
                    "стиснуто": packed,
                    "розпаковано": len(payload),
                    **({"вміст": describe_bytes(payload, depth=1)} if i < MAX_STREAMS_DESCRIBED else {}),
                }
                for i, (offset, packed, payload) in enumerate(streams)
            ]
    return out


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = np.bincount(np.frombuffer(data, dtype=np.uint8), minlength=256)
    p = counts[counts > 0] / len(data)
    return round(float(-(p * np.log2(p)).sum()), 2)


def _round_numbers(data: bytes) -> dict[str, list[dict[str, Any]]]:
    """Числа з кількома знаками після коми в діапазонах `ROUND_RANGES` — по
    всьому буферу, на кожному зсуві, double і float, обидва порядки байтів.

    Групується за (тип, значення): коефіцієнт диска лежить один-два рази, а
    повтор сотні разів — ознака чогось іншого (крок сітки, висота)."""
    groups: dict[tuple[str, str, float], dict[str, Any]] = {}
    for label, dtype, decimals in (
        ("double LE", "<f8", ROUND_DECIMALS_DOUBLE),
        ("double BE", ">f8", ROUND_DECIMALS_DOUBLE),
        ("float LE", "<f4", ROUND_DECIMALS_FLOAT),
        ("float BE", ">f4", ROUND_DECIMALS_FLOAT),
    ):
        width = np.dtype(dtype).itemsize
        for shift in range(width):
            count = (len(data) - shift) // width
            if count <= 0:
                continue
            # Сирі байти дають і NaN — це не помилка, лише не число.
            with np.errstate(invalid="ignore", over="ignore"):
                values = np.frombuffer(data, dtype=dtype, count=count, offset=shift).astype(np.float64)
            for range_name, low, high in ROUND_RANGES:
                hits = np.nonzero((values >= low) & (values <= high))[0]
                for index in hits:
                    value = float(values[index])
                    if not _is_round(value, decimals, single=width == 4):
                        continue
                    shown = round(value, decimals)
                    key = (range_name, label, shown)
                    group = groups.setdefault(key, {"тип": label, "значення": shown, "разів": 0, "зсуви": []})
                    group["разів"] += 1
                    if len(group["зсуви"]) < 3:
                        group["зсуви"].append(shift + int(index) * width)
    out: dict[str, list[dict[str, Any]]] = {}
    for range_name, _low, _high in ROUND_RANGES:
        found = [g for (name, _t, _v), g in groups.items() if name == range_name]
        found.sort(key=lambda g: (g["разів"], g["значення"]))
        for group in found:
            group["зсуви"].sort()
        out[range_name] = found[:ROUND_GROUPS_MAX]
    return out


def _is_round(value: float, decimals: int, *, single: bool) -> bool:
    if not math.isfinite(value):
        return False
    rounded = round(value, decimals)
    if single:
        # float32 «1.247» не дорівнює double 1.247 — порівнюємо в float32.
        return struct.unpack("<f", struct.pack("<f", rounded))[0] == value
    return rounded == value


def _compressed_streams(data: bytes) -> list[tuple[int, int, bytes]]:
    """zlib/gzip-потоки всередині файлу: (зсув, стиснутих байтів, вміст).

    Заголовок `78 9C` трапляється і випадково; справжнім вважається лише
    потік, що дійшов до кінця (`eof`) або вперся в межу розпаковки."""
    found: list[tuple[int, int, bytes]] = []
    view = memoryview(data)
    skip_until = 0
    total = 0
    for match in _STREAM_HEAD.finditer(data):
        start = match.start()
        if start < skip_until:
            continue
        inflater = zlib.decompressobj(47)  # 32 + 15: zlib або gzip за заголовком
        try:
            payload = inflater.decompress(view[start:], MAX_STREAM_OUT)
        except zlib.error:
            continue
        if len(payload) < 16 or not (inflater.eof or len(payload) >= MAX_STREAM_OUT):
            continue
        packed = len(data) - start - len(inflater.unused_data) - len(inflater.unconsumed_tail)
        found.append((start, packed, payload))
        skip_until = start + max(packed, 1)
        total += len(payload)
        if len(found) >= MAX_STREAMS or total >= MAX_STREAMS_TOTAL_OUT:
            break
    return found


# Скільки байтів з початку диска звіряти між файлами. Параметри диска
# (висота, діаметр, усадка) логічно лежать у заголовку, до розкладки робіт.
HEADER_COMPARE_BYTES = 4096
_NUMBER_TYPES = (("float LE", "<f"), ("float BE", ">f"), ("double LE", "<d"), ("double BE", ">d"))


def compare_headers(files: list[tuple[str, str]]) -> dict[str, Any]:
    """Звірити початок кількох `.blk` число-в-число на тих самих зсувах.

    `files` — (назва, шлях). Друга спроба знайти усадку (04.11.26): у ПММА
    усадки немає, тож там, де Sum3D тримає коефіцієнт, у диску ПММА має
    стояти рівно 1.0, а в цирконієвому — 1.1–1.4 (будь-яке, не лише кругле:
    з етикетки партії буває й 1.2365). Друге — місця, де в усіх дисках
    число 0.5–2.0, але різне: так виглядав би коефіцієнт, якщо ПММА в
    вибірці немає. Лише числа."""
    heads: list[tuple[str, bytes]] = []
    for name, path in files:
        try:
            with open(path, "rb") as handle:
                heads.append((name, handle.read(HEADER_COMPARE_BYTES)))
        except OSError:
            continue
    if len(heads) < 2:
        return {"примітка": "потрібно ≥2 читабельних дисків"}
    shortest = min(len(data) for _name, data in heads)
    one_vs_shrink: list[dict[str, Any]] = []
    differing: list[dict[str, Any]] = []
    for label, fmt in _NUMBER_TYPES:
        width = struct.calcsize(fmt)
        for offset in range(0, shortest - width + 1):
            values = [struct.unpack_from(fmt, data, offset)[0] for _name, data in heads]
            if not all(math.isfinite(v) for v in values):
                continue
            row = {
                "зсув": offset,
                "тип": label,
                "значення": {name: round(v, 6) for (name, _data), v in zip(heads, values)},
            }
            has_one = any(v == 1.0 for v in values)
            if has_one and any(
                PLAUSIBLE_LOW <= v <= PLAUSIBLE_HIGH or 0.71 <= v <= 0.91 for v in values
            ):
                one_vs_shrink.append(row)
            elif all(0.5 <= v <= 2.0 for v in values) and len({round(v, 6) for v in values}) > 1:
                differing.append(row)
    return {
        "байтів_звірено": shortest,
        "одиниця_проти_усадки": one_vs_shrink[:40],
        "різні_числа_0_5_до_2": differing[:40],
        "різних_усього": len(differing),
    }


def common_words(paths: Iterable[str]) -> list[str]:
    """Слова латиницею (≥4 символи), що є в КОЖНОМУ з файлів, — разом зі
    стиснутими потоками. Назви ключів формату однакові в усіх дисках, а назва
    STL з іменем пацієнта — своя в кожному; тому віддаємо лише перетин, і
    кличе це інструмент лише для ≥3 дисків."""
    common: Optional[set[str]] = None
    for path in paths:
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_READ_BYTES)
        except OSError:
            return []
        words = _words(data)
        for _offset, _packed, payload in _compressed_streams(data):
            words |= _words(payload)
        common = words if common is None else common & words
    return sorted(common or ())


def _words(data: bytes) -> set[str]:
    return {
        match.group().decode(encoding, "replace")
        for pattern, encoding in _WORD_PATTERNS
        for match in pattern.finditer(data)
    }


def _numbers_after(
    data: bytes, start: int, window: int = 64, *, big_endian: bool = False
) -> list[dict[str, Any]]:
    """Правдоподібні коефіцієнти одразу за маркером: double, float і число
    текстом. Зсув — від кінця маркера, щоб звірити між кількома файлами.
    Після слова в UTF-16BE (Qt) і числа, найімовірніше, big-endian."""
    chunk = data[start:start + window]
    order = ">" if big_endian else "<"
    found: list[dict[str, Any]] = []
    for offset in range(0, max(0, len(chunk) - 3)):
        if offset + 8 <= len(chunk):
            (value,) = struct.unpack_from(f"{order}d", chunk, offset)
            if _plausible(value):
                found.append({"тип": "double", "зсув": offset, "значення": round(value, 6)})
        (single,) = struct.unpack_from(f"{order}f", chunk, offset)
        if _plausible(single):
            found.append({"тип": "float", "зсув": offset, "значення": round(single, 5)})
    for match in _ASCII_NUMBER.finditer(chunk):
        number = _as_number(match.group().decode("ascii"))
        if number is not None and _plausible(number):
            found.append({"тип": "текст", "зсув": match.start(), "значення": number})
    return found[:12]
