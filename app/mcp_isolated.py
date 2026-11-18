"""Важкі інструменти MCP — в ОКРЕМОМУ процесі (18.11.26).

Чому. 18.11.26 о 02:08 два виклики `kmill_sum3d_files kind=cam` (розбір
десятків мегабайт `.cam`: zlib-потоки, «круглі» double, слова) поклали весь
прод на дві хвилини: `/mail` 121 с, `/api/notify-state` 85 с, кнопки
оператора — «Сервер не відповів вчасно». Інструмент ішов у threadpool ТОГО
САМОГО процесу, і чистий Python під GIL голодив кожен запит (у лозі
«license 14 с» — ознака GIL, не бази). Таймаут клієнта потік не зупиняє, тож
повтор додав другий такий самий.

Що робимо. Інструмент, чий `Tool.isolate(args)` каже «важко», запускається
окремим процесом того самого exe (`KuubMill.exe --mcp-tool <запит> <відповідь>`;
у dev — `python -m app.mcp_isolated`), з пріоритетом нижче звичайного:
  * свій GIL — CRM і Sum3D на цьому ПК не чекають;
  * межа часу `TIMEOUT_SECONDS` — процес убивається, а не висить годинами
    (disc-hint 17.11.26 висів 3 год 9 хв саме так);
  * одночасно лише ОДИН — другий важкий запит отримує «зайнято» одразу.

Обмін — двома файлами JSON у тимчасовій теці, не stdout: зібраний exe без
консолі (`sys.stdout = None`). Процес відкриває СВОЮ сесію до тієї самої бази
(SQLite у WAL — читачі не заважають записувачам) і нічого не пише.

Ізолювати можна лише інструмент, який не читає живого стану процесу CRM
(знімок верстатів, кеші в памʼяті): у дочірньому процесі цього стану немає, і
відповідь була б тихо порожньою. Тому `isolate` — предикат на аргументи, а не
прапорець на весь інструмент (`kmill_sum3d_files kind=board_disc` читає
верстати з памʼяті й лишається вдома).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from app.services.mcp_common import Tool, ToolError

logger = logging.getLogger(__name__)

# Клієнт MCP рве зʼєднання приблизно за хвилину; відповідь, що прийде пізніше,
# нікому не потрібна, а процес жер би ЦП далі.
TIMEOUT_SECONDS = 50

# Тести ганяють інструменти на базі в памʼяті, якої дочірній процес не бачить —
# там виконання лишається вдома (`tests/conftest.py`).
INLINE = False

_busy = threading.Lock()

_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_CREATE_NO_WINDOW = 0x08000000

ARG = "--mcp-tool"


def should_isolate(tool: Tool, args: dict) -> bool:
    if INLINE or tool.isolate is None:
        return False
    try:
        return bool(tool.isolate(args))
    except Exception:  # noqa: BLE001 — сумнів = окремий процес, не прод
        logger.warning("MCP: предикат ізоляції %s упав — запускаю окремо", tool.name, exc_info=True)
        return True


def _command(request: Path, result: Path) -> tuple[list[str], Optional[str]]:
    if getattr(sys, "frozen", False):
        return [sys.executable, ARG, str(request), str(result)], None
    root = str(Path(__file__).resolve().parents[1])
    return [sys.executable, "-m", "app.mcp_isolated", ARG, str(request), str(result)], root


def run(
    tool: Tool,
    args: dict,
    *,
    timeout: float = TIMEOUT_SECONDS,
    command: Callable[[Path, Path], tuple[list[str], Optional[str]]] = _command,
) -> dict[str, Any]:
    """Виконати інструмент окремим процесом і повернути його відповідь.

    Помилки — `ToolError` з наступним кроком: той, хто питає, бачить текст, а
    не обірване зʼєднання."""
    if not _busy.acquire(blocking=False):
        raise ToolError(
            f"зараз уже йде інший важкий запит MCP — «{tool.name}» не запущено, щоб не "
            "навантажувати ПК цеху. Повтори за хвилину."
        )
    try:
        with tempfile.TemporaryDirectory(prefix="kmill-mcp-") as tmp:
            request = Path(tmp) / "request.json"
            result = Path(tmp) / "result.json"
            request.write_text(
                json.dumps({"tool": tool.name, "args": args}, ensure_ascii=False), encoding="utf-8"
            )
            cmd, cwd = command(request, result)
            flags = (_CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY_CLASS) if os.name == "nt" else 0
            started = _now()
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.error("MCP: процес %s (pid %s) не зупинився після kill", tool.name, proc.pid)
                logger.warning(
                    "MCP: %s не вклався в %.0f с — окремий процес зупинено (аргументи %s)",
                    tool.name, timeout, sorted(args),
                )
                raise ToolError(
                    f"«{tool.name}» не вклався в {timeout:.0f} с — його окремий процес "
                    "зупинено, CRM це не зачепило. Звузь запит: менший `limit`, `brand`, "
                    "`sum3d_id` чи `days`."
                ) from None
            logger.info("MCP: %s в окремому процесі — %.1f с, код %s", tool.name, _now() - started, code)
            return _read_result(tool.name, result, code)
    finally:
        _busy.release()


def _now() -> float:
    import time

    return time.monotonic()


def _read_result(name: str, path: Path, code: int) -> dict[str, Any]:
    try:
        answer = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("MCP: %s в окремому процесі завершився кодом %s без відповіді", name, code)
        raise ToolError(
            f"«{name}» в окремому процесі завершився без відповіді (код {code}) — "
            "подробиці в logs/mcp-tool.log CRM (kmill_log з log=поточний не бачить цей файл)."
        ) from None
    if answer.get("ok"):
        payload = answer.get("result")
        return payload if isinstance(payload, dict) else {"відповідь": payload}
    if answer.get("tool_error"):
        raise ToolError(str(answer["tool_error"]))
    logger.error("MCP: %s упав в окремому процесі: %s", name, answer.get("crash"))
    raise ToolError(
        f"«{name}» упав в окремому процесі: {answer.get('crash') or 'причина невідома'} — "
        "подробиці в logs/mcp-tool.log."
    )


# ── Дочірній процес ─────────────────────────────────────────────────────────


def _configure_child_logging() -> None:
    from logging.handlers import RotatingFileHandler

    from app.config import DATA_DIR

    logs = Path(DATA_DIR) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / "mcp-tool.log", maxBytes=512_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def _write_answer(path: Path, answer: dict[str, Any]) -> None:
    part = path.with_suffix(".part")
    part.write_text(json.dumps(answer, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(part, path)


def run_child(request: str, result: str, *, session_factory: Optional[Callable[[], Any]] = None) -> int:
    """Тіло дочірнього процесу: прочитати запит, виконати, записати відповідь."""
    out = Path(result)
    try:
        if session_factory is None:
            _configure_child_logging()
        asked = json.loads(Path(request).read_text(encoding="utf-8"))
        name = str(asked.get("tool") or "")
        args = asked.get("args") if isinstance(asked.get("args"), dict) else {}
        from app.services.mcp_tools import TOOLS_BY_NAME

        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            _write_answer(out, {"ok": False, "tool_error": f"інструмента «{name}» немає"})
            return 2
        if session_factory is None:
            from app.db import SessionLocal

            session_factory = SessionLocal
        with session_factory() as db:
            try:
                payload = tool.run(db, args)
            except ToolError as exc:
                _write_answer(out, {"ok": False, "tool_error": str(exc)})
                return 0
        _write_answer(out, {"ok": True, "result": payload})
        return 0
    except Exception as exc:  # noqa: BLE001 — відповідь мусить лягти й при збої
        logger.exception("MCP: збій в окремому процесі")
        try:
            _write_answer(out, {"ok": False, "crash": f"{type(exc).__name__}: {exc}"})
        except OSError:
            logger.exception("MCP: не записав відповідь")
        return 1


if __name__ == "__main__":  # dev: python -m app.mcp_isolated --mcp-tool <запит> <відповідь>
    if len(sys.argv) >= 4 and sys.argv[1] == ARG:
        sys.exit(run_child(sys.argv[2], sys.argv[3]))
    sys.exit(2)
