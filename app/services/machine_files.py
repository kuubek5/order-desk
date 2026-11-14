"""Розвідка файлів на ПК верстата через агента — ЛИШЕ ЧИТАННЯ (власник 14.11.26).

Мета: знайти, де програма верстата тримає технічні дані — мотогодини фрез,
заміни, шпиндель, температуру. OPC-UA закритий (логіна немає), тож дивимось
файли: агент (`agent/discover.go`) віддає перелік файлів у теках програми
верстата (`/discover`) і початок одного текстового файлу (`/peek`).

Як шукаємо «корисний» файл, не знаючи формату:

* за назвою — слова tool / spindle / runtime / life / … (`HINT_WORDS`);
* за ЗМІНОЮ — перелік запам'ятовується, і наступний виклик показує, які
  файли змінились. Що змінилось, поки верстат різав, — лічильники й журнали;
  що змінилось після заміни фрези — облік фрез.

Звідси нічого не йде ні в базу, ні на екрани: це інструмент розвідки для MCP.
Літери поза латиницею в шляхах і рядках затираються — у назвах робіт можуть
бути імена пацієнтів.
"""

from __future__ import annotations

import base64
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

LIST_TIMEOUT = 45
PEEK_TIMEOUT = 20
MAX_CANDIDATES = 60
MAX_CHANGED = 60
MAX_LINE_CHARS = 160

HINT_WORDS = (
    "tool", "tch", "spindle", "runtime", "life", "wear", "hour", "temp", "stat",
    "count", "maint", "service", "error", "alarm", "log",
)
TEXT_EXT = frozenset({
    ".ini", ".cfg", ".conf", ".config", ".log", ".txt", ".xml", ".json", ".csv", ".tab",
    ".dat", ".prm", ".par", ".lst", ".properties", ".yaml", ".yml", ".tol", ".tool",
})
_NON_ASCII = re.compile(r"[^\x00-\x7F]")
_NUMERIC_VALUE = re.compile(r"^\s*([A-Za-z_][\w .\-]{0,60}?)\s*[=:]\s*(-?\d+(?:[.,]\d+)?)\s*$")

_lock = threading.Lock()
_last: dict[int, dict[str, Any]] = {}   # machine_id → {"at", "paths", "marks"}


class AgentError(Exception):
    """Агент не віддав дані; текст каже, що робити далі."""


def mask(text: str) -> str:
    return _NON_ASCII.sub("•", text)


def _get(target: Any, path: str, params: dict[str, str], timeout: int) -> dict[str, Any]:
    import requests

    if not getattr(target, "is_agent", False):
        raise AgentError("у верстата немає агента (підключений через VNC) — файли прочитати нічим")
    try:
        resp = requests.get(
            f"http://{target.host}:{target.port}{path}", params=params,
            headers={"X-Agent-Token": target.agent_token}, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise AgentError(f"агент не відповів: {exc.__class__.__name__}") from None
    if resp.status_code == 404:
        raise AgentError("агент старий: розвідки файлів у ньому немає — поставити нову версію агента")
    if resp.status_code == 429:
        raise AgentError("агент уже обходить теки — повторити за пів хвилини")
    if resp.status_code == 503:
        raise AgentError("спостереження на верстаті призупинено (значок у треї)")
    if resp.status_code != 200:
        raise AgentError(f"агент відмовив ({resp.status_code}): {mask(resp.text.strip())[:120]}")
    try:
        data = resp.json()
    except ValueError:
        raise AgentError("агент відповів не JSON") from None
    if not isinstance(data, dict):
        raise AgentError("агент відповів не об'єктом")
    return data


def fetch_tops(target: Any) -> dict[str, list[str]]:
    """Назви тек верхнього рівня — як тека програми зветься на цьому ПК."""
    tops = _get(target, "/discover", {"tops": "1"}, LIST_TIMEOUT).get("tops") or {}
    return {mask(str(parent)): [mask(str(n)) for n in names] for parent, names in tops.items()}


def _ext(path: str) -> str:
    name = path.replace("/", "\\").rsplit("\\", 1)[-1]
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def _hints(path: str) -> list[str]:
    name = path.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    stem = name.rsplit(".", 1)[0] if "." in name else name   # `.log` — не слово в назві
    return [w for w in HINT_WORDS if w in stem]


def _when(stamp: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(stamp).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def summarize(
    files: list[dict[str, Any]], previous: Optional[dict[str, tuple[int, str]]], *, now: datetime
) -> dict[str, Any]:
    """Кандидати за назвою й свіжістю та зміни проти попереднього переліку.
    Номер у «№» — той, яким файл потім просять у `peek`."""
    paths = [str(f.get("path") or "") for f in files]
    marks = {p: (int(f.get("size") or 0), str(f.get("mtime") or "")) for p, f in zip(paths, files)}
    by_ext: dict[str, int] = {}
    scored = []
    for number, path in enumerate(paths, start=1):
        ext = _ext(path)
        by_ext[ext or "(без розширення)"] = by_ext.get(ext or "(без розширення)", 0) + 1
        if ext not in TEXT_EXT:
            continue
        size, stamp = marks[path]
        hints = _hints(path)
        when = _when(stamp)
        fresh = when is not None and now - when <= timedelta(hours=24)
        score = 3 * len(hints) + (2 if fresh else 0) + (1 if ext in (".ini", ".log") else 0)
        if size and score:
            scored.append((score, stamp, number, path, size, hints, fresh))
    scored.sort(key=lambda row: row[1], reverse=True)   # свіжіші вище…
    scored.sort(key=lambda row: -row[0])                # …у межах того самого балу
    candidates = [
        {"№": number, "файл": mask(path), "розмір": size, "змінено": stamp,
         "слова": hints, "свіжий": fresh}
        for _score, stamp, number, path, size, hints, fresh in scored[:MAX_CANDIDATES]
    ]
    out: dict[str, Any] = {
        "файлів": len(paths),
        "за_розширенням": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])[:25]),
        "кандидати": candidates,
    }
    if previous is not None:
        changed = []
        for number, path in enumerate(paths, start=1):
            before = previous.get(path)
            if before == marks[path]:
                continue
            changed.append({
                "№": number, "файл": mask(path),
                "було": {"розмір": before[0], "змінено": before[1]} if before else "не було",
                "стало": {"розмір": marks[path][0], "змінено": marks[path][1]},
            })
        gone = [mask(p) for p in previous if p not in marks]
        out["змінилось_з_минулого_переліку"] = changed[:MAX_CHANGED]
        out["змінилось_усього"] = len(changed)
        out["зникло"] = gone[:MAX_CHANGED]
    return out


def discover(target: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Перелік файлів верстата з кандидатами й змінами проти минулого виклику."""
    data = _get(target, "/discover", {}, LIST_TIMEOUT)
    files = [f for f in (data.get("files") or []) if isinstance(f, dict)]
    key = int(getattr(target, "machine_id", 0) or 0)
    with _lock:
        before = _last.get(key)
    report = summarize(files, before["marks"] if before else None, now=now or datetime.now())
    paths = [str(f.get("path") or "") for f in files]
    marks = {p: (int(f.get("size") or 0), str(f.get("mtime") or "")) for p, f in zip(paths, files)}
    with _lock:
        _last[key] = {"at": now or datetime.now(), "paths": paths, "marks": marks}
    return {
        "теки_програми": [mask(str(r)) for r in data.get("roots") or []],
        "тек_пройдено": data.get("dirs"),
        "не_все_влізло": bool(data.get("truncated")),
        "обхід_мс": data.get("elapsed_ms"),
        "минулий_перелік": before["at"].strftime("%Y-%m-%d %H:%M:%S") if before else None,
        **report,
    }


def path_by_number(target: Any, number: int) -> str:
    key = int(getattr(target, "machine_id", 0) or 0)
    with _lock:
        last = _last.get(key)
    if last is None:
        raise AgentError("спершу перелік (mode=list): номер файлу береться з нього")
    if not 1 <= number <= len(last["paths"]):
        raise AgentError(f"номера {number} в останньому переліку немає (1…{len(last['paths'])})")
    return str(last["paths"][number - 1])


def decode(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def peek(target: Any, path: str, *, start: int = 0, limit: int = 80) -> dict[str, Any]:
    """Початок файлу: рядки (літери поза латиницею затерто) і числові ключі."""
    data = _get(target, "/peek", {"path": path}, PEEK_TIMEOUT)
    try:
        raw = base64.b64decode(str(data.get("data") or ""), validate=True)
    except ValueError:
        raise AgentError("агент віддав зіпсований вміст") from None
    if b"\x00" in raw[:4096] and not raw.startswith(b"\xff\xfe"):
        return {"файл": mask(path), "розмір": data.get("size"), "змінено": data.get("mtime"),
                "двійковий": True}
    lines = decode(raw).splitlines()
    numeric: list[str] = []
    for line in lines:
        match = _NUMERIC_VALUE.match(line)
        if match and len(numeric) < 200:
            numeric.append(f"{mask(match.group(1).strip())} = {match.group(2)}")
    return {
        "файл": mask(path),
        "розмір": data.get("size"),
        "змінено": data.get("mtime"),
        "прочитано_не_весь": bool(data.get("truncated")),
        "рядків_прочитано": len(lines),
        "з_рядка": start,
        "рядки": [mask(line)[:MAX_LINE_CHARS] for line in lines[start:start + limit]],
        "числові_ключі": numeric,
    }
