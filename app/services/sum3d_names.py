"""Повна назва програми верстата з проєкту Sum3D (власник 12.11.26).

Верстати нового покоління (CORiTEC 250i/150i) не пишуть назву програми в
заголовок вікна, а на екрані JOBS вона малими літерами (`pmma16_14-a4-x81_…`),
для яких у читача екрана немає еталонів. Тому з екрана приходить лише
`2026-11-12_01-20-12.iso`, і табло показувало дату замість диска.

Sum3D ID (час) і дату ми знаємо, а отже знаємо й проєкт
`<Cam-work>/<дата>_<Sum3D>.cam`. Sum3D сам складає назву програми і пише її в
проєкт; шукаємо в ньому рядок `<щось>_<дата>_<Sum3D>` (ASCII або UTF-16).
Знайшли — це та сама назва, що на екрані верстата; ні — лишається дата.

Проєкт — десятки МБ, тож читаємо шматками й лише у фоні: `lookup` віддає те,
що вже знайдено, а пошук запускає окремим потоком (один на назву).
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_SCAN_BYTES = 96 * 1024 * 1024
_CHUNK = 8 * 1024 * 1024
_OVERLAP = 512
_HEAD_CHARS = re.compile(r"[A-Za-z0-9.\-]")
_HEAD_MAX = 80
_BARE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<id>\d{2}-\d{2}-\d{2})(?:\.iso)?$", re.I)

_found: dict[str, Optional[str]] = {}      # "дата_id" → повна назва або None (не знайшли)
_running: set[str] = set()
_lock = threading.Lock()
_root: Optional[str] = None


def configure(projects_root: Optional[str]) -> None:
    """Тека проєктів (Налаштування → Шляхи). Кличе фоновий прохід машин."""
    global _root
    _root = (projects_root or "").strip() or None


def bare_parts(iso_name: Optional[str]) -> Optional[tuple[str, str]]:
    """(дата, Sum3D) для голої назви `дата_час.iso`; інакше None."""
    match = _BARE.match((iso_name or "").strip())
    return (match.group("date"), match.group("id")) if match else None


def full_name(iso_name: Optional[str]) -> Optional[str]:
    """Повна назва для голої `дата_час.iso`, якщо вже знайдена; інакше None.
    Ще не шукали — запускає пошук у фоні й теж відповідає None."""
    parts = bare_parts(iso_name)
    if parts is None or _root is None:
        return None
    key = f"{parts[0]}_{parts[1]}"
    with _lock:
        if key in _found:
            return _found[key]
        if key in _running:
            return None
        _running.add(key)
    threading.Thread(target=_search_into_cache, args=(key, parts, _root),
                     name="kuubmill-sum3d-name", daemon=True).start()
    return None


def _search_into_cache(key: str, parts: tuple[str, str], root: str) -> None:
    try:
        name = find_in_project(Path(root) / f"{key}.cam", *parts)
    except OSError as exc:
        logger.info("Назва програми: проєкт %s не прочитано: %s", key, exc)
        name = None
    with _lock:
        _running.discard(key)
        _found[key] = name
    logger.info("Назва програми для %s: %s", key, name or "у проєкті не знайдено")


def find_in_project(path: Path, date: str, sum3d_id: str) -> Optional[str]:
    """`<голова>_<дата>_<Sum3D>.iso` з проєкту або None. Читає шматками."""
    tail = f"_{date}_{sum3d_id}"
    needles = [(tail.encode("ascii"), 1), (tail.encode("utf-16-le"), 2)]
    read = 0
    previous = b""
    with open(path, "rb") as handle:
        while read < MAX_SCAN_BYTES:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            data = previous + chunk
            for needle, width in needles:
                start = 0
                while True:
                    at = data.find(needle, start)
                    if at < 0:
                        break
                    head = _head_before(data, at, width)
                    if head:
                        return f"{head}_{date}_{sum3d_id}.iso"
                    start = at + 1
            previous = data[-_OVERLAP:]
    return None


def _head_before(data: bytes, at: int, width: int) -> str:
    """Символи назви перед `_дата_час`: латиниця, цифри, `.`, `-`, `_`.
    Порожньо або сама дата — не назва (так виглядає шлях до файлу проєкту)."""
    chars: list[str] = []
    pos = at - width
    while pos >= 0 and len(chars) < _HEAD_MAX:
        unit = data[pos:pos + width]
        if width == 2 and unit[1:] != b"\x00":
            break
        ch = chr(unit[0])
        if not (_HEAD_CHARS.match(ch) or ch == "_"):
            break
        chars.append(ch)
        pos -= width
    head = "".join(reversed(chars)).strip("_.-")
    if not head or not re.search(r"[A-Za-z]", head):
        return ""
    return head
