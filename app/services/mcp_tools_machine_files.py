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
    tail = bool(args.get("tail"))
    grep = str(args.get("grep") or "")[:200]
    width = _int_arg(args, "width", default=machine_files.MAX_LINE_CHARS, low=40,
                     high=machine_files.WIDE_LINE_CHARS)
    start = _int_arg(args, "start", default=0, low=0, high=1_000_000)
    limit = _int_arg(args, "limit", default=80, low=1, high=400)
    try:
        if mode == "tops":
            return {"верстат": target.name, "теки_верхнього_рівня": machine_files.fetch_tops(target)}
        if mode == "list":
            find = str(args.get("find") or "")[:120]
            return {"верстат": target.name, **machine_files.discover(target, find=find)}
        if mode in ("peek", "zip"):
            if args.get("file") in (None, ""):
                raise ToolError(f"для `{mode}` потрібен `file` — номер «№» з останнього переліку")
            number = _int_arg(args, "file", default=0, low=1, high=100_000)
            path = machine_files.path_by_number(target, number)
            if mode == "peek":
                return {"верстат": target.name, **machine_files.peek(
                    target, path, start=start, limit=limit, tail=tail, grep=grep, width=width)}
            if args.get("entry") in (None, ""):
                return {"верстат": target.name, **machine_files.zip_entries(target, path)}
            entry = _int_arg(args, "entry", default=0, low=1, high=100_000)
            return {"верстат": target.name, **machine_files.zip_peek(
                target, path, entry, start=start, limit=limit, tail=tail, grep=grep, width=width)}
    except machine_files.AgentError as exc:
        raise ToolError(f"{target.name}: {exc}") from None
    raise ToolError("`mode` — tops, list, peek або zip (типово list)")


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
            "двічі: до й після різання чи заміни фрези); find — підрядок шляху "
            "(`.zip`, `logs`) — ще й усі такі файли з «№». mode=peek (file — «№» з "
            "переліку, start, limit) — рядки файлу й числові ключі; tail=true — КІНЕЦЬ "
            "файлу (свіже в журналі), start тоді рахується від кінця; grep — лише "
            "рядки за регулярним виразом; width — ширина рядка до 1000 (типово 160). "
            "mode=zip (file — «№» архіву) — перелік файлів архіву старих журналів; "
            "з entry (номер з цього переліку) — рядки одного з них (tail, grep, width "
            "так само). Кінець файлу й архіви — лише агент з 16.11.26. Агент лише "
            "читає, зі спільним доступом, не замикає файли верстата й не виходить "
            "за теки програми. Літери поза латиницею затерто (імена пацієнтів)."
        ),
        schema={
            "type": "object",
            "properties": {
                "machine": {"type": "string", "description": "Підрядок назви верстата (boris)."},
                "mode": {"type": "string", "description": "tops, list (типово), peek або zip."},
                "find": {"type": "string", "description": "Для list: підрядок шляху — показати всі такі файли."},
                "entry": {"type": "integer", "minimum": 1,
                          "description": "Для zip: номер файлу з переліку архіву."},
                "tail": {"type": "boolean", "description": "Для peek/zip: читати кінець файлу."},
                "grep": {"type": "string", "description": "Для peek/zip: лише рядки за регулярним виразом."},
                "width": {"type": "integer", "minimum": 40, "maximum": 1000,
                          "description": "Для peek/zip: ширина рядка (типово 160)."},
                "file": {"type": "integer", "minimum": 1,
                         "description": "Для peek і zip: номер «№» з останнього переліку цього верстата."},
                "start": {"type": "integer", "minimum": 0, "description": "Для peek: з якого рядка (типово 0)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 400,
                          "description": "Для peek: скільки рядків (типово 80)."},
            },
            "additionalProperties": False,
        },
        run=tool_machine_files,
    ),
)
