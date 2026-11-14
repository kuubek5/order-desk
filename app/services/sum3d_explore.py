"""Глибока розвідка Sum3D — ЛИШЕ ЧИТАННЯ (власник 14.11.26).

Мета: зрозуміти, як влаштований Sum3D у цеху — теки, стратегії, налаштування,
що програма вміє приймати ззовні, — щоб вирішити, які зв'язки з KuubMill
можливі. Звідси нічого не йде ні в базу, ні на екрани: це інструмент для MCP.

Що вміє (`kmill_sum3d_explore`):

* `roots`   — які теки розвідуємо (встановлення з Налаштувань, спільні
              стратегії, тека поруч із дисками);
* `map`     — карта тек: скільки файлів, яких, якого розміру, що найновіше;
* `files`   — перелік файлів теки з номерами (за підрядком чи розширенням);
* `schema`  — ключі й теги ТЕКСТОВИХ файлів одного виду (стратегії, .ini,
              .xml): скільки разів, межі чисел, приклади значень;
* `file`    — один файл за номером: рядки й числові ключі або розбір
              двійкового (формат, заголовок, рядки в ньому);
* `exe`     — програми й бібліотеки: версії та рядки про імпорт, exocad,
              командний рядок, авторозстановку — що Sum3D вміє приймати;
* `assoc`   — чим Windows відкриває .blk/.cam/.stl (чи можна відкрити диск
              у Sum3D одним запуском).

Безпека:

* файли відкриваються через `shared_read` — Sum3D може писати, перейменовувати
  й видаляти їх, поки ми читаємо (звичайний `open` це блокує);
* теки робіт (`CAM-work`, `export`, диски, копії) не обходимо взагалі;
* назв STL не віддаємо, літери поза латиницею затираємо (імена пацієнтів);
* межі часу, кількості й розміру на кожен обхід; жодного запису.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.fs_probe import PRESENT, path_state
from app.services.machine_files import decode, mask
from app.shared_read import read_shared

SKIP_DIRS = frozenset({
    "cam-work", "cam-blanks", "export", "backup", "backups", "$recycle.bin", "system volume information",
})
WALK_BUDGET_S = 15.0
MAX_ENTRIES = 60000
TEXT_READ = 256 * 1024
BIN_READ = 2 * 1024 * 1024
EXE_READ = 48 * 1024 * 1024
MAX_SCHEMA_FILES = 400
MAX_KEYS = 150
MAX_LINE = 160

_INI = re.compile(r"^\s*([A-Za-z_][\w .\-]{0,60}?)\s*[=:]\s*(.{0,80})$")
_XML_TAG = re.compile(r"<([A-Za-z_][\w.\-:]{0,60})(?:\s[^<>]*)?>([^<>]{0,80})</\1>")
_XML_ATTR = re.compile(r"\s([A-Za-z_][\w.\-:]{0,60})=\"([^\"]{0,60})\"")
_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_ASCII_RUN = re.compile(rb"[\x20-\x7e]{6,120}")
_WIDE_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){6,120}")
# Що шукаємо в програмі: сліди того, що Sum3D приймає ззовні.
HINTS = (
    "exocad", "constructioninfo", "3shape", "dentalcad", "hotfolder", "hot folder", "autoimport",
    "auto import", "importfile", "openfile", "loadproject", "nesting", "autonest", "batch",
    "commandline", "command line", "cmdline", "/silent", "plugin", "script", "macro", "automation",
    "namedpipe", "socket", "http://", "https://", "strateg", "template", ".blk", ".cam", ".stl", ".iso",
)
_OWN_BINARY = re.compile(r"sum|cam|cim|dent|easy|nest|post|mill|launch", re.IGNORECASE)
ASSOC_EXTS = (".blk", ".cam", ".stl", ".constructioninfo", ".iso")

_lock = threading.Lock()
_last: list[str] = []          # шляхи останнього переліку: номер «№» → файл


class ExploreError(Exception):
    """Розвідка не може відповісти; текст каже, що робити далі."""


def roots(db: Session) -> list[tuple[str, str]]:
    """(назва, тека) — усе, що розвідуємо. Порядок сталий: номер теки в
    інструменті береться звідси."""
    from app.services import sum3d_deep, sum3d_install
    from app.settings_store import get_setting

    found: list[tuple[str, str]] = [(i.name, i.path) for i in sum3d_install.read_installs(db)]
    strategies = (get_setting(db, sum3d_install.STRATEGIES_KEY) or "").strip()
    if strategies:
        found.append(("стратегії", strategies))
    blanks = (get_setting(db, "cam_blanks_path") or "").strip()
    found += [("тека Sum3D", path) for path in sum3d_deep.settings_roots(blanks)]
    seen: set[str] = set()
    out = []
    for name, path in found:
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            out.append((name, path))
    return out


def _root(db: Session, number: int) -> tuple[str, str]:
    known = roots(db)
    if not known:
        raise ExploreError("тек Sum3D не задано: Налаштування → Обладнання → «Програма Sum3D»")
    if not 1 <= number <= len(known):
        raise ExploreError(f"`root` — номер теки 1…{len(known)} (what=roots показує перелік)")
    name, path = known[number - 1]
    if path_state(path) != PRESENT:
        raise ExploreError(f"тека «{name}» недоступна або її немає")
    return name, path


def walk(root: str, *, depth_max: int = 8) -> tuple[list[tuple[str, str, int, float]], bool]:
    """[(повний шлях, відносний, байт, час зміни)] без тек робіт і без STL;
    другий елемент — чи обхід обірвано межею часу чи кількості."""
    started = time.monotonic()
    out: list[tuple[str, str, int, float]] = []
    stack: list[tuple[str, str, int]] = [(root, "", 0)]
    while stack:
        if time.monotonic() - started > WALK_BUDGET_S or len(out) >= MAX_ENTRIES:
            return out, True
        folder, rel, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in SKIP_DIRS and depth < depth_max:
                        stack.append((entry.path, f"{rel}{entry.name}/", depth + 1))
                elif entry.is_file(follow_symlinks=False) and not entry.name.lower().endswith(".stl"):
                    info = entry.stat()
                    out.append((entry.path, f"{rel}{entry.name}", info.st_size, info.st_mtime))
            except OSError:
                continue
    return out, False


def _ext(name: str) -> str:
    return os.path.splitext(name.lower())[1] or "(без розширення)"


def _when(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def roots_report(db: Session) -> dict[str, Any]:
    return {"теки": [
        {"№": i, "назва": name, "тека": mask(path), "стан": "є" if path_state(path) == PRESENT else "немає/недоступна"}
        for i, (name, path) in enumerate(roots(db), start=1)
    ] or "не задано: Налаштування → Обладнання → «Програма Sum3D»"}


def tree(db: Session, number: int, depth: int = 2) -> dict[str, Any]:
    """Карта теки: на кожну підтеку до `depth` рівнів — файли, розмір, розширення."""
    name, path = _root(db, number)
    files, cut = walk(path)
    folders: dict[str, dict[str, Any]] = {}
    for _full, rel, size, stamp in files:
        parts = rel.split("/")[:-1][:depth]
        key = "/".join(parts) or "(корінь)"
        row = folders.setdefault(key, {"n": 0, "size": 0, "ext": Counter(), "new": 0.0})
        row["n"] += 1
        row["size"] += size
        row["ext"][_ext(rel)] += 1
        row["new"] = max(row["new"], stamp)
    rows = [
        {"тека": mask(key), "файлів": row["n"], "мб": round(row["size"] / 1024**2, 1),
         "розширення": dict(row["ext"].most_common(6)), "найновіше": _when(row["new"])}
        for key, row in sorted(folders.items(), key=lambda kv: -kv[1]["n"])
    ]
    return {"назва": name, "файлів": len(files), "обрізано": cut, "теки": rows[:120]}


def listing(db: Session, number: int, *, query: str = "", ext: str = "", limit: int = 150) -> dict[str, Any]:
    """Файли теки з номерами: за підрядком шляху й/або розширенням, новіші вище."""
    global _last
    name, path = _root(db, number)
    files, cut = walk(path)
    needle, want = query.strip().lower(), ext.strip().lower()
    if want and not want.startswith("."):
        want = "." + want
    hits = [f for f in files if (not needle or needle in f[1].lower()) and (not want or _ext(f[1]) == want)]
    hits.sort(key=lambda f: -f[3])
    shown = hits[:limit]
    with _lock:
        _last = [f[0] for f in shown]
    return {
        "назва": name, "знайдено": len(hits), "обрізано": cut,
        "файли": [{"№": i, "файл": mask(rel), "байт": size, "змінено": _when(stamp)}
                  for i, (_full, rel, size, stamp) in enumerate(shown, start=1)],
    }


def as_text(data: bytes) -> Optional[str]:
    """Текст файлу або None для двійкового (нульові байти без позначки UTF-16)."""
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        return decode(data)
    return None if b"\x00" in data[:4096] else decode(data)


def pairs(text: str) -> list[tuple[str, str]]:
    """(ключ, значення) з тексту: рядки `ключ=значення`, теги й атрибути XML."""
    out: list[tuple[str, str]] = []
    if "<" in text and ">" in text:
        out += [(m.group(1), m.group(2).strip()) for m in _XML_TAG.finditer(text)]
        out += [(m.group(1), m.group(2).strip()) for m in _XML_ATTR.finditer(text)]
    for line in text.splitlines():
        if "<" in line:
            continue
        match = _INI.match(line)
        if match:
            out.append((match.group(1).strip(), match.group(2).strip()))
    return out


def schema(db: Session, number: int, *, query: str = "", ext: str = "") -> dict[str, Any]:
    """Ключі текстових файлів одного виду разом: що в них задається й у яких
    межах. Так видно параметри стратегій, не відкриваючи кожну."""
    name, path = _root(db, number)
    files, cut = walk(path)
    needle, want = query.strip().lower(), ext.strip().lower()
    if want and not want.startswith("."):
        want = "." + want
    if not needle and not want:
        raise ExploreError("для `schema` вкажіть `ext` (xml, ini, str…) або `query` — інакше це вся тека")
    hits = [f for f in files if (not needle or needle in f[1].lower()) and (not want or _ext(f[1]) == want)]
    started = time.monotonic()
    keys: dict[str, dict[str, Any]] = {}
    read = binary = 0
    for full, _rel, size, _stamp in hits[:MAX_SCHEMA_FILES]:
        if time.monotonic() - started > WALK_BUDGET_S:
            cut = True
            break
        try:
            text = as_text(read_shared(full, TEXT_READ))
        except OSError:
            continue
        if text is None:
            binary += 1
            continue
        read += 1
        for key, value in pairs(text):
            row = keys.setdefault(key, {"n": 0, "lo": None, "hi": None, "samples": []})
            row["n"] += 1
            if _NUMBER.match(value):
                number_value = float(value.replace(",", "."))
                row["lo"] = number_value if row["lo"] is None else min(row["lo"], number_value)
                row["hi"] = number_value if row["hi"] is None else max(row["hi"], number_value)
            elif value and len(row["samples"]) < 4 and len(value) <= 40 and mask(value) not in row["samples"]:
                row["samples"].append(mask(value))
    top = sorted(keys.items(), key=lambda kv: -kv[1]["n"])[:MAX_KEYS]
    return {
        "назва": name, "файлів_підходить": len(hits), "розібрано_текстових": read, "двійкових": binary,
        "обрізано": cut or len(hits) > MAX_SCHEMA_FILES, "ключів": len(keys),
        "ключі": [{"ключ": mask(key), "разів": row["n"],
                   "числа": None if row["lo"] is None else [row["lo"], row["hi"]],
                   "приклади": row["samples"]} for key, row in top],
    }


def strings_in(data: bytes, limit: int = 80) -> list[str]:
    """Читабельні латинські рядки двійкового файлу (ASCII та UTF-16)."""
    seen: list[str] = []
    for match in _ASCII_RUN.finditer(data):
        seen.append(match.group().decode("ascii"))
        if len(seen) >= limit * 4:
            break
    for match in _WIDE_RUN.finditer(data):
        seen.append(match.group().decode("utf-16-le"))
        if len(seen) >= limit * 8:
            break
    out: list[str] = []
    for text in seen:
        text = text.strip()
        if len(text) >= 6 and re.search(r"[A-Za-z]{4}", text) and text not in out:
            out.append(text[:120])
    return out[:limit]


def look(number: int, *, start: int = 0, limit: int = 80) -> dict[str, Any]:
    """Один файл із останнього переліку: текст рядками або розбір двійкового."""
    from app.services import sum3d_files

    with _lock:
        known = list(_last)
    if not known:
        raise ExploreError("спершу перелік (what=files): номер файлу береться з нього")
    if not 1 <= number <= len(known):
        raise ExploreError(f"номера {number} в останньому переліку немає (1…{len(known)})")
    path = known[number - 1]
    try:
        size = os.path.getsize(path)
        text_data = read_shared(path, TEXT_READ)
    except OSError as exc:
        raise ExploreError(f"файл не прочитано: {exc.__class__.__name__}") from None
    base: dict[str, Any] = {"файл": mask(os.path.basename(path)), "байт": size}
    text = as_text(text_data)
    if text is not None:
        lines = text.splitlines()
        numeric = [f"{mask(k)} = {v}" for k, v in pairs(text) if _NUMBER.match(v)][:200]
        return {**base, "вид": "текст", "прочитано_не_весь": size > len(text_data), "рядків": len(lines),
                "з_рядка": start, "рядки": [mask(line)[:MAX_LINE] for line in lines[start:start + limit]],
                "числові_ключі": numeric}
    try:
        data = read_shared(path, BIN_READ)
    except OSError as exc:
        raise ExploreError(f"файл не прочитано: {exc.__class__.__name__}") from None
    return {**base, "вид": "двійковий", "прочитано_не_весь": size > len(data),
            "заголовок_hex": data[:16].hex(" "), **sum3d_files.describe_bytes(data),
            "рядки_в_ньому": strings_in(data, limit)}


def hints_in(data: bytes) -> dict[str, list[str]]:
    """Рядки програми за темами `HINTS`: до 8 на тему, без повторів."""
    low_hints = [h.lower() for h in HINTS]
    out: dict[str, list[str]] = {}
    runs = [m.group().decode("ascii") for m in _ASCII_RUN.finditer(data)]
    runs += [m.group().decode("utf-16-le") for m in _WIDE_RUN.finditer(data)]
    for text in runs:
        low = text.lower()
        for hint in low_hints:
            if hint in low:
                bucket = out.setdefault(hint, [])
                clean = text.strip()[:120]
                if len(bucket) < 8 and clean not in bucket:
                    bucket.append(clean)
    return out


def programs(db: Session, number: int) -> dict[str, Any]:
    """Програми й бібліотеки теки: версії та рядки про імпорт, exocad, командний
    рядок, авторозстановку. Лише у власних файлах Sum3D — у чужих бібліотеках
    (Qt, VC++) слово «import» є в кожній і нічого не каже."""
    from app.services.sum3d_install import file_version

    name, path = _root(db, number)
    files, cut = walk(path, depth_max=2)
    binaries = [f for f in files if _ext(f[1]) in (".exe", ".dll")]
    started = time.monotonic()
    rows = []
    for full, rel, size, stamp in sorted(binaries, key=lambda f: (_ext(f[1]) != ".exe", f[1].lower()))[:200]:
        row: dict[str, Any] = {"файл": mask(rel), "байт": size, "змінено": _when(stamp), "версія": file_version(full)}
        own = _ext(rel) == ".exe" or _OWN_BINARY.search(os.path.basename(rel))
        if own and time.monotonic() - started < WALK_BUDGET_S * 2:
            try:
                found = hints_in(read_shared(full, EXE_READ))
            except OSError:
                found = {}
            if found:
                row["рядки"] = found
        rows.append(row)
    return {"назва": name, "обрізано": cut, "програм_і_бібліотек": len(binaries),
            "файли": [r for r in rows if "рядки" in r] + [r for r in rows if "рядки" not in r][:60]}


def associations() -> dict[str, Any]:
    """Чим Windows відкриває файли Sum3D: якщо команда має `"%1"`, диск чи
    проєкт можна відкрити запуском програми з файлом. Лише читання реєстру."""
    if sys.platform != "win32":
        return {"помилка": "лише Windows"}
    import winreg

    def default(root: Any, key: str) -> Optional[str]:
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, "")
                return str(value) if value else None
        except OSError:
            return None

    def verbs(progid: str) -> dict[str, str]:
        """Усі дії типу файлу (open, edit, …) з командами — зведений вигляд HKCR."""
        found: dict[str, str] = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{progid}\\shell") as shell:
                for index in range(20):
                    try:
                        verb = winreg.EnumKey(shell, index)
                    except OSError:
                        break
                    command = default(winreg.HKEY_CLASSES_ROOT, f"{progid}\\shell\\{verb}\\command")
                    if command:
                        found[mask(verb)] = mask(command)
        except OSError:
            return found
        return found

    out = []
    for ext in ASSOC_EXTS:
        row: dict[str, Any] = {"розширення": ext}
        progid = default(winreg.HKEY_CLASSES_ROOT, ext)
        if not progid:
            row["стан"] = "не призначено"
        else:
            row["тип"] = mask(progid)
            row["дії"] = verbs(progid) or "команд немає"
        out.append(row)
    return {"асоціації": out}
