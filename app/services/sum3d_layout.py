"""Розкладка іконок Sum3D: коли й чому вона «злітає» (власник 16.11.26). Лише читання.

Sum3D (MFC) зберігає панелі інструментів у `CAMWINC.INI` поруч зі своєю
програмою — секції `[CamToolbarsFlat-BarN]` (позиція й видимість кожної панелі),
`[CamToolbarsFlat-Summary]` (роздільність екрана, при якій зберігали) — і
ПЕРЕПИСУЄ файл при кожному закритті. Перемагає останнє закрите вікно; інша
роздільність (RustDesk) чи невикладене вікно перекладає панелі, і так вони й
зберігаються. На ПК цеху дві Sum3D (`C:\\iCAMV5smart`, `D:\\SUM`), у кожної свій
файл.

Тут — лише спостереження, щоб знайти причину на даних, а не вгадувати: раз на
кілька секунд дивимось час зміни файлу; змінився — читаємо (`open_shared`, не
заважаючи Sum3D) і, якщо розкладка інша, ніж була, пишемо подію: що змінилось,
роздільність, які Sum3D (з якої теки) зараз запущені й які щойно закрились.
Запуски й закриття `camwinc.exe` теж пишемо. У файли Sum3D не пишемо нічого;
відновлення еталона — окреме рішення власника.

Список недавніх файлів і блокування з того самого INI НЕ читаються в історію:
там назви проєктів.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

INI_NAME = "CAMWINC.INI"
PROCESS_NAME = "camwinc.exe"
MAX_EVENTS = 400
# Sum3D пише INI вже ПІСЛЯ того, як зникло вікно: закриття, що було не давніше
# за це, вважаємо причетним до зміни розкладки.
RECENT_CLOSE_SECONDS = 90.0
_SECTION_RE = re.compile(r"^\[CamToolbarsFlat-(?P<name>Bar\d+|Summary)\]$", re.I)

_lock = Lock()
# Стан у пам'яті: файл → (mtime_ns, size); процеси pid → (exe, create_time).
_seen_files: dict[str, tuple[int, int]] = {}
_processes: dict[int, tuple[str, float]] = {}
_recent_closes: list[tuple[float, str]] = []
_primed = False
_state_cache: Optional[dict[str, Any]] = None


def state_path() -> Path:
    from app.config import DATA_DIR

    return Path(DATA_DIR) / "sum3d_layout.json"


# ── розбір ───────────────────────────────────────────────────────────────────


def parse_layout(text: str) -> dict[str, Any]:
    """`CAMWINC.INI` → панелі, ряди доків і роздільність. Інші секції ігноруються."""
    sections: dict[str, dict[str, str]] = {}
    current: Optional[dict[str, str]] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            match = _SECTION_RE.match(line)
            current = sections.setdefault(match.group("name"), {}) if match else None
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip()

    bars: dict[str, dict[str, Any]] = {}
    docks: dict[str, list[list[str]]] = {}
    for name, values in sections.items():
        if name.lower() == "summary":
            continue
        bar_id = values.get("BarID")
        if not bar_id:
            continue
        if "Bars" in values:
            # Док: `Bar#i` — панелі по порядку, `0` відділяє ряди.
            rows: list[list[str]] = [[]]
            for i in range(_int(values.get("Bars"), 0) or 0):
                member = values.get(f"Bar#{i}", "0")
                if member == "0":
                    if rows[-1]:
                        rows.append([])
                else:
                    rows[-1].append(member)
            docks[bar_id] = [row for row in rows if row]
            continue
        bars[bar_id] = {
            "visible": values.get("Visible", "1") != "0",
            "x": _int(values.get("XPos"), None),
            "y": _int(values.get("YPos"), None),
            "floating": values.get("Docking", "1") == "0",
        }
    summary = sections.get("Summary") or sections.get("summary") or {}
    return {
        "bars": bars,
        "docks": docks,
        "screen": [_int(summary.get("ScreenCX"), None), _int(summary.get("ScreenCY"), None)],
    }


def _int(value: Optional[str], default: Optional[int]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def signature(layout: dict[str, Any]) -> str:
    """Відбиток розкладки: лише те, що бачить оператор (видимі панелі, їх місця, ряди)."""
    visible = sorted((bar_id, b["x"], b["y"], b["floating"])
                     for bar_id, b in layout["bars"].items() if b["visible"])
    docks = sorted((dock, rows) for dock, rows in layout["docks"].items())
    blob = json.dumps([visible, docks], ensure_ascii=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def summarize(layout: dict[str, Any]) -> dict[str, Any]:
    visible = [b for b in layout["bars"].values() if b["visible"]]
    return {
        "відбиток": signature(layout),
        "екран": layout["screen"],
        "панелей_видно": len(visible),
        "плаваючих": sum(1 for b in visible if b["floating"]),
        "рядів_у_доках": {dock: len(rows) for dock, rows in layout["docks"].items()},
    }


def diff(old: dict[str, Any], new: dict[str, Any], limit: int = 25) -> list[dict[str, Any]]:
    """Які панелі змінили місце чи видимість."""
    out: list[dict[str, Any]] = []
    for bar_id in sorted(set(old["bars"]) | set(new["bars"]), key=lambda v: _int(v, 0) or 0):
        a, b = old["bars"].get(bar_id), new["bars"].get(bar_id)
        if a == b:
            continue
        out.append({"панель": bar_id, "було": _bar_text(a), "стало": _bar_text(b)})
        if len(out) >= limit:
            break
    return out


def _bar_text(bar: Optional[dict[str, Any]]) -> str:
    if bar is None:
        return "немає"
    if not bar["visible"]:
        return "сховано"
    where = "плаває" if bar["floating"] else "у доку"
    return f"{where} x={bar['x']} y={bar['y']}"


# ── стан ─────────────────────────────────────────────────────────────────────


def _load_state() -> dict[str, Any]:
    global _state_cache
    if _state_cache is None:
        try:
            _state_cache = json.loads(state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _state_cache = {}
        _state_cache.setdefault("files", {})
        _state_cache.setdefault("events", [])
    return _state_cache


def _save_state(state: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("Розкладка Sum3D: не вдалось зберегти історію в %s", path, exc_info=True)


def _event(state: dict[str, Any], **fields: Any) -> None:
    fields = {"коли": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **fields}
    state["events"].append(fields)
    del state["events"][:-MAX_EVENTS]


# ── процеси ──────────────────────────────────────────────────────────────────


def _running() -> dict[int, tuple[str, float]]:
    try:
        import psutil
    except ImportError:
        return {}
    out: dict[int, tuple[str, float]] = {}
    for proc in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        info = proc.info
        if (info.get("name") or "").lower() != PROCESS_NAME:
            continue
        out[int(info["pid"])] = (str(info.get("exe") or "?"), float(info.get("create_time") or 0.0))
    return out


def _track_processes(state: dict[str, Any], now: float) -> bool:
    global _primed
    current = _running()
    changed = False
    if _primed:
        # Перший прохід після старту KuubMill лише запам'ятовує, що вже
        # запущено: інакше кожен рестарт писав би «запуск» давно відкритої Sum3D.
        for pid, (exe, _) in current.items():
            if pid not in _processes:
                _event(state, подія="запуск", exe=exe, pid=pid, запущено_всього=len(current))
                changed = True
        for pid, (exe, _) in _processes.items():
            if pid not in current:
                _recent_closes.append((now, exe))
                _event(state, подія="закриття", exe=exe, pid=pid, лишилось=len(current))
                changed = True
    _primed = True
    _processes.clear()
    _processes.update(current)
    del _recent_closes[:-20]
    return changed


# ── прохід ───────────────────────────────────────────────────────────────────


def ini_files(install_paths: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for folder in install_paths:
        candidate = Path(folder) / INI_NAME
        try:
            if candidate.is_file():
                out.append(candidate)
        except OSError:
            continue
    return out


def _read(path: Path) -> Optional[str]:
    from app.shared_read import open_shared

    try:
        with open_shared(str(path)) as handle:
            data = handle.read(512 * 1024)
    except OSError:
        return None
    return data.decode("cp1252", errors="replace")


def watch_once(install_paths: Iterable[str], *, now: Optional[float] = None) -> int:
    """Один прохід сторожа: процеси й файли розкладки. Повертає число нових подій."""
    moment = time.time() if now is None else now
    with _lock:
        state = _load_state()
        before = len(state["events"])
        dirty = _track_processes(state, moment)
        for path in ini_files(install_paths):
            key = str(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            mark = (stat.st_mtime_ns, stat.st_size)
            if _seen_files.get(key) == mark:
                continue
            _seen_files[key] = mark
            text = _read(path)
            if text is None:
                continue
            layout = parse_layout(text)
            if not layout["bars"]:
                continue
            sig = signature(layout)
            entry = state["files"].setdefault(key, {"layouts": {}})
            seen = entry["layouts"].setdefault(sig, {
                "перший_раз": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "разів": 0,
                **summarize(layout), "layout": layout,
            })
            previous = entry.get("current")
            if previous == sig:
                continue
            seen["разів"] += 1
            seen["останній_раз"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            old_layout = entry["layouts"].get(previous, {}).get("layout") if previous else None
            entry["current"] = sig
            dirty = True
            if old_layout is None:
                _event(state, подія="перший_знімок", файл=key, **summarize(layout))
                continue
            closed = sorted({exe for when, exe in _recent_closes if moment - when <= RECENT_CLOSE_SECONDS})
            _event(
                state, подія="розкладка_змінилась", файл=key, було=previous, **summarize(layout),
                змінено=diff(old_layout, layout),
                запущені=sorted({exe for exe, _ in _processes.values()}),
                щойно_закрито=closed,
            )
        if dirty:
            _save_state(state)
        return len(state["events"]) - before


def report(limit: int = 60) -> dict[str, Any]:
    """Для MCP: розкладки по файлах (скільки разів була кожна) і останні події."""
    with _lock:
        state = _load_state()
        files = []
        for key, entry in state["files"].items():
            layouts = sorted(entry["layouts"].items(), key=lambda kv: -kv[1].get("разів", 0))
            files.append({
                "файл": key,
                "зараз": entry.get("current"),
                "розкладки": [
                    {k: v for k, v in item.items() if k != "layout"} | {"відбиток": sig}
                    for sig, item in layouts
                ],
            })
        return {
            "файли": files,
            "запущено_зараз": sorted({exe for exe, _ in _processes.values()}),
            "події": state["events"][-limit:][::-1],
            "пояснення": (
                "Sum3D переписує CAMWINC.INI при закритті. «розкладка_змінилась» — після якого "
                "закриття (щойно_закрито) і при якій роздільності (екран) панелі стали іншими; "
                "«запущені» — скільки Sum3D і з яких тек було відкрито водночас. Лише читання."
            ),
        }


def reset_for_tests() -> None:
    global _state_cache, _primed
    with _lock:
        _seen_files.clear()
        _processes.clear()
        _recent_closes.clear()
        _state_cache = None
        _primed = False
