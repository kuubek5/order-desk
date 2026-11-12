"""Встановлення Sum3D і спільні теки — ЛИШЕ читання (власник 12.11.26).

У цеху ДВІ версії Sum3D (новіша й старіша), і обидві в роботі. Спільні в них
стратегії (тека однієї з версій) і тека дисків. Адміністратор задає це в
Налаштування → Обладнання → «Програма Sum3D»:

* `sum3d_installs` — JSON-список `{"name", "path"}`, по рядку на версію;
* `sum3d_strategies_path` — спільна тека стратегій;
* тека дисків (`cam_blanks_path`) і тека проєктів (`sum3d_projects_path`)
  уже задані в інших розділах — тут їх лише показуємо.

**Тут немає жодної операції запису у файли** — лише перелік тек, імена,
розмір, час зміни й версія exe з його ресурсу. Стереже
`tests/test_program_folders.py`. Вміст журналів і назви STL назовні не йдуть.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.fs_probe import PRESENT, UNREACHABLE, path_state

INSTALLS_KEY = "sum3d_installs"
STRATEGIES_KEY = "sum3d_strategies_path"
MAX_INSTALLS = 6
SCAN_DEPTH = 3
SCAN_BUDGET_S = 6.0
MAX_ENTRIES = 20000
CONFIG_EXTS = (".xml", ".ini", ".cfg", ".json", ".dat", ".lib", ".mat", ".tool", ".tlb", ".db", ".str", ".stg")
LOG_EXTS = (".log",)


@dataclass(frozen=True)
class Install:
    name: str
    path: str


def read_installs(db: Session) -> list[Install]:
    from app.settings_store import get_setting

    raw = get_setting(db, INSTALLS_KEY) or ""
    try:
        items = json.loads(raw) if raw.strip() else []
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()[:60]
            path = str(item.get("path") or "").strip()[:400]
            if path:
                out.append(Install(name or "Sum3D", path))
    return out[:MAX_INSTALLS]


def clean_installs(pairs: list[tuple[str, str]]) -> list[Install]:
    """Рядки форми → список без порожніх шляхів, лапок і дублів."""
    seen: set[str] = set()
    out: list[Install] = []
    for name, path in pairs:
        clean = (path or "").strip().strip('"')[:400]
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(Install((name or "").strip()[:60] or f"Sum3D {len(out) + 1}", clean))
    return out[:MAX_INSTALLS]


def installs_json(items: list[Install]) -> str:
    return json.dumps([{"name": i.name, "path": i.path} for i in items], ensure_ascii=False)


def file_version(path: str) -> Optional[str]:
    """Версія з ресурсу exe (Windows); None — не Windows чи ресурсу немає."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    try:
        api = ctypes.WinDLL("version")
        size = api.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not api.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)) or not length.value:
            return None

        class _Fixed(ctypes.Structure):
            _fields_ = [(n, wintypes.DWORD) for n in (
                "sig", "struc", "ms", "ls", "pms", "pls", "mask", "flags", "os", "type", "sub", "dms", "dls")]

        fixed = ctypes.cast(ptr, ctypes.POINTER(_Fixed)).contents
        return f"{fixed.ms >> 16}.{fixed.ms & 0xFFFF}.{fixed.ls >> 16}.{fixed.ls & 0xFFFF}"
    except (OSError, AttributeError, ValueError):
        return None


@dataclass
class FolderProbe:
    path: str
    state: str = "absent"
    error: str = ""
    dirs: list[str] = field(default_factory=list)
    exts: Counter = field(default_factory=Counter)
    files: int = 0
    size: int = 0
    exes: list[tuple[str, Optional[str]]] = field(default_factory=list)
    configs: list[tuple[str, int, datetime]] = field(default_factory=list)
    logs: list[tuple[str, int, datetime]] = field(default_factory=list)
    newest: list[tuple[str, datetime]] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: int = 0

    def summary(self) -> str:
        if not self.path:
            return "шлях не задано"
        if self.state == UNREACHABLE:
            return "тека недоступна (мережа чи права)"
        if self.state != PRESENT:
            return "теки не знайдено"
        if self.error:
            return f"не прочитано: {self.error}"
        main = next((e for e in self.exes if "sum3d" in e[0].lower()), self.exes[0] if self.exes else None)
        head = f"{main[0]} {main[1] or ''}".strip() + " · " if main else ""
        more = "+" if self.truncated else ""
        return f"{head}файлів: {self.files}{more}, тек: {len(self.dirs)}"


def probe_folder(path: Optional[str], *, with_versions: bool = True) -> FolderProbe:
    """Що лежить у теці (на SCAN_DEPTH вглиб, з межею часу). Нічого не змінює."""
    raw = (path or "").strip()
    probe = FolderProbe(path=raw)
    if not raw:
        return probe
    started = time.monotonic()
    probe.state = path_state(raw)
    if probe.state != PRESENT:
        return probe
    stack: list[tuple[str, str, int]] = [(raw, "", 0)]
    try:
        while stack:
            if time.monotonic() - started > SCAN_BUDGET_S or probe.files >= MAX_ENTRIES:
                probe.truncated = True
                break
            folder, rel, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                if depth == 0:
                    raise
                continue
            for entry in entries:
                name = f"{rel}{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    if depth == 0:
                        probe.dirs.append(entry.name)
                    if depth < SCAN_DEPTH:
                        stack.append((entry.path, name + "/", depth + 1))
                    continue
                low = entry.name.lower()
                ext = os.path.splitext(low)[1] or "(без розширення)"
                if ext == ".stl":
                    probe.exts[ext] += 1  # назви STL — імена пацієнтів, не збираємо
                    probe.files += 1
                    continue
                try:
                    info = entry.stat()
                except OSError:
                    continue
                probe.files += 1
                probe.size += info.st_size
                probe.exts[ext] += 1
                when = datetime.fromtimestamp(info.st_mtime)
                probe.newest.append((name, when))
                if ext == ".exe" and depth <= 1:
                    probe.exes.append((name, file_version(entry.path) if with_versions else None))
                elif ext in CONFIG_EXTS:
                    probe.configs.append((name, info.st_size, when))
                elif ext in LOG_EXTS:
                    probe.logs.append((name, info.st_size, when))
    except OSError as exc:
        probe.error = f"{exc.__class__.__name__}: {exc.strerror or exc}"
    probe.dirs.sort(key=str.lower)
    probe.newest = sorted(probe.newest, key=lambda x: x[1], reverse=True)[:15]
    probe.logs.sort(key=lambda x: x[2], reverse=True)
    probe.configs.sort(key=lambda x: x[0].lower())
    probe.elapsed_ms = int((time.monotonic() - started) * 1000)
    return probe


def _probe_dict(p: FolderProbe, limit: int) -> dict[str, Any]:
    return {
        "тека": p.path or None,
        "стан": p.summary(),
        "файлів": p.files,
        "мб": round(p.size / 1024**2, 1),
        "обрізано": p.truncated,
        "теки_першого_рівня": p.dirs[:60],
        "розширення": dict(p.exts.most_common(30)),
        "програми_exe": [{"файл": n, "версія": v} for n, v in p.exes[:20]],
        "налаштування_й_бібліотеки": [
            {"файл": n, "байт": s, "змінено": w.strftime("%Y-%m-%d %H:%M")} for n, s, w in p.configs[:limit]
        ],
        "журнали": [
            {"файл": n, "байт": s, "змінено": w.strftime("%Y-%m-%d %H:%M")} for n, s, w in p.logs[:limit]
        ],
        "змінювалось_останнім": [
            {"файл": n, "змінено": w.strftime("%Y-%m-%d %H:%M")} for n, w in p.newest[:10]
        ],
        "прохід_мс": p.elapsed_ms,
    }


def overview(db: Session, limit: int = 40) -> dict[str, Any]:
    """Усі встановлення й спільні теки — для MCP. Лише читання."""
    from app.settings_store import get_setting, get_sum3d_projects_path

    installs = read_installs(db)
    out: dict[str, Any] = {
        "встановлення": [
            {"назва": i.name, **_probe_dict(probe_folder(i.path), limit)} for i in installs
        ] or "не задано (Налаштування → Програма Sum3D)",
    }
    strategies = (get_setting(db, STRATEGIES_KEY) or "").strip()
    out["стратегії"] = _probe_dict(probe_folder(strategies, with_versions=False), limit) if strategies else "не задано"
    blanks = (get_setting(db, "cam_blanks_path") or "").strip()
    out["тека_дисків"] = blanks or None
    out["тека_проєктів"] = (get_sum3d_projects_path(db) or "").strip() or None
    out["примітка"] = "Лише читання: імена, розміри, час, версії exe. Вміст і назви STL не віддаються."
    return out
