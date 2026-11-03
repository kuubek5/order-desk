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
* **Бінарне читаємо лише біля маркерів.** У бінарному файлі може бути сітка
  STL, а координати вершин у мм лягають і в діапазон коефіцієнтів (1.1–1.4) —
  суцільний скан дав би тисячі хибних «кандидатів». Тому числа шукаються лише
  поруч зі словами-маркерами (shrink, scale, factor…).
"""

from __future__ import annotations

import io
import os
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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
_ASCII_NUMBER = re.compile(rb"[+-]?\d+[.,]\d+")


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

    `brand` — підрядок назви диска (`monolith`, `emo`), без регістру. Час —
    `mtime`: тут він лише впорядковує вибірку для огляду, не рахує нічого.
    `DirEntry.stat()` на Windows бере дані з самого переліку теки — тисячі
    файлів не дають тисяч мережевих запитів.
    """
    base = Path(root)
    wanted = brand.strip().lower()
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
    return out


def describe_bytes(data: bytes, *, depth: int = 0) -> dict[str, Any]:
    if data[:4] == b"PK\x03\x04" and depth == 0:
        return _describe_zip(data)
    text = _as_text(data)
    if text is not None:
        return _describe_text(text)
    return _describe_binary(data)


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


def _describe_binary(data: bytes) -> dict[str, Any]:
    markers: list[dict[str, Any]] = []
    for pattern, width in ((_ASCII_WORD, 1), (_UTF16_WORD, 2)):
        for match in pattern.finditer(data):
            word = match.group().decode("ascii" if width == 1 else "utf-16-le", "replace")
            if not KEYWORDS.search(word):
                continue
            end = match.end()
            markers.append({
                "маркер": word,
                "зсув": match.start(),
                "кандидати": _numbers_after(data, end),
            })
            if len(markers) >= 60:
                break
    return {
        "формат": "бінарний",
        "розмір": len(data),
        "маркери": markers,
    }


def _numbers_after(data: bytes, start: int, window: int = 64) -> list[dict[str, Any]]:
    """Правдоподібні коефіцієнти одразу за маркером: double, float і число
    текстом. Зсув — від кінця маркера, щоб звірити між кількома файлами."""
    chunk = data[start:start + window]
    found: list[dict[str, Any]] = []
    for offset in range(0, max(0, len(chunk) - 3)):
        if offset + 8 <= len(chunk):
            (value,) = struct.unpack_from("<d", chunk, offset)
            if _plausible(value):
                found.append({"тип": "double", "зсув": offset, "значення": round(value, 6)})
        (single,) = struct.unpack_from("<f", chunk, offset)
        if _plausible(single):
            found.append({"тип": "float", "зсув": offset, "значення": round(single, 5)})
    for match in _ASCII_NUMBER.finditer(chunk):
        number = _as_number(match.group().decode("ascii"))
        if number is not None and _plausible(number):
            found.append({"тип": "текст", "зсув": match.start(), "значення": number})
    return found[:12]
