"""Інструмент MCP: розвідка файлів на ПК верстата через агента. Лише читання."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services import machine_files
from app.services.mcp_common import Tool, ToolError, _int_arg


def tool_machine_files(db: Session, args: dict) -> dict[str, Any]:
    """Які файли програма верстата тримає на своєму ПК і що в них
    (`machine_files`). Без `machine` — лише перелік верстатів, без мережі."""
    from app.services.machines import list_machines, target_of

    machines = list_machines(db)
    wanted = str(args.get("machine") or "").strip().lower()
    if not wanted:
        return {"верстати": [
            {"верстат": m.name, "агент": target_of(m).is_agent, "увімкнено": bool(m.enabled)}
            for m in machines
        ], "далі": "вказати `machine` (підрядок назви) і mode: tops, list або peek"}
    found = [m for m in machines if wanted in m.name.lower()]
    if len(found) != 1:
        names = ", ".join(m.name for m in (found or machines))
        raise ToolError(f"`machine` має вказати рівно один верстат; є: {names}")
    target = target_of(found[0])
    mode = str(args.get("mode") or "list").strip().lower()
    try:
        if mode == "tops":
            return {"верстат": target.name, "теки_верхнього_рівня": machine_files.fetch_tops(target)}
        if mode == "list":
            return {"верстат": target.name, **machine_files.discover(target)}
        if mode == "peek":
            raw = args.get("file")
            if raw in (None, ""):
                raise ToolError("для `peek` потрібен `file` — номер «№» з останнього переліку")
            number = _int_arg(args, "file", default=0, low=1, high=100_000)
            start = _int_arg(args, "start", default=0, low=0, high=1_000_000)
            limit = _int_arg(args, "limit", default=80, low=1, high=400)
            path = machine_files.path_by_number(target, number)
            return {"верстат": target.name, **machine_files.peek(target, path, start=start, limit=limit)}
    except machine_files.AgentError as exc:
        raise ToolError(f"{target.name}: {exc}") from None
    raise ToolError("`mode` — tops, list або peek (типово list)")


MACHINE_FILE_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="kmill_machine_files",
        description=(
            "Розвідка файлів на ПК верстата через агента — де програма верстата "
            "тримає мотогодини фрез, заміни, шпиндель, температуру. Без `machine` — "
            "перелік верстатів і чи є агент. mode=tops — назви тек верхнього рівня "
            "(як зветься тека програми). mode=list (типово) — файли в теках програми "
            "верстата: кандидати за назвою (tool, spindle, runtime…) і свіжістю, "
            "лічильник за розширенням і ЩО ЗМІНИЛОСЬ з минулого переліку (викликати "
            "двічі: до й після різання чи заміни фрези). mode=peek (file — номер «№» "
            "з переліку, start, limit) — рядки файлу й числові ключі. Агент лише "
            "читає, зі спільним доступом, не замикає файли верстата й не виходить "
            "за теки програми. Літери поза латиницею затерто (імена пацієнтів)."
        ),
        schema={
            "type": "object",
            "properties": {
                "machine": {"type": "string", "description": "Підрядок назви верстата (boris)."},
                "mode": {"type": "string", "description": "tops, list (типово) або peek."},
                "file": {"type": "integer", "minimum": 1,
                         "description": "Для peek: номер «№» з останнього переліку цього верстата."},
                "start": {"type": "integer", "minimum": 0, "description": "Для peek: з якого рядка (типово 0)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 400,
                          "description": "Для peek: скільки рядків (типово 80)."},
            },
            "additionalProperties": False,
        },
        run=tool_machine_files,
    ),
)
