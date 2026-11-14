"""Інструмент MCP: глибока розвідка Sum3D (`sum3d_explore`). Лише читання."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services import sum3d_explore
from app.services.mcp_common import Tool, ToolError, _int_arg

_WHAT = ("roots", "map", "files", "schema", "file", "exe", "assoc")


def tool_sum3d_explore(db: Session, args: dict) -> dict[str, Any]:
    what = str(args.get("what") or "roots").strip().lower()
    if what not in _WHAT:
        raise ToolError("`what` — " + ", ".join(_WHAT) + " (типово roots)")
    try:
        if what == "roots":
            return sum3d_explore.roots_report(db)
        if what == "assoc":
            return sum3d_explore.associations()
        if what == "file":
            if args.get("file") in (None, ""):
                raise ToolError("для `file` потрібен `file` — номер «№» з останнього переліку (what=files)")
            return sum3d_explore.look(
                _int_arg(args, "file", default=0, low=1, high=100_000),
                start=_int_arg(args, "start", default=0, low=0, high=1_000_000),
                limit=_int_arg(args, "limit", default=80, low=1, high=400),
            )
        root = _int_arg(args, "root", default=1, low=1, high=50)
        query, ext = str(args.get("query") or ""), str(args.get("ext") or "")
        if what == "map":
            return sum3d_explore.tree(db, root, _int_arg(args, "depth", default=2, low=1, high=5))
        if what == "files":
            return sum3d_explore.listing(db, root, query=query, ext=ext,
                                         limit=_int_arg(args, "limit", default=150, low=1, high=400))
        if what == "schema":
            return sum3d_explore.schema(db, root, query=query, ext=ext)
        return sum3d_explore.programs(db, root)
    except sum3d_explore.ExploreError as exc:
        raise ToolError(str(exc)) from None


SUM3D_EXPLORE_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="kmill_sum3d_explore",
        description=(
            "Глибока розвідка Sum3D лише на читання: як влаштовані теки, стратегії й налаштування і "
            "що програма вміє приймати ззовні. what=roots — які теки розвідуємо (встановлення з "
            "Налаштувань, спільні стратегії, тека поруч із дисками), з номерами. what=map (root, "
            "depth) — карта теки: файли, розмір, розширення, найновіше по підтеках. what=files (root, "
            "query, ext, limit) — файли з номерами «№», новіші вище. what=schema (root, ext або query) "
            "— ключі й теги текстових файлів одного виду разом: скільки разів, межі чисел, приклади "
            "(параметри стратегій). what=file (file — «№» з переліку, start, limit) — рядки й числові "
            "ключі або розбір двійкового файлу. what=exe (root) — програми й бібліотеки: версії та "
            "рядки про імпорт, exocad, командний рядок, авторозстановку. what=assoc — чим Windows "
            "відкриває .blk/.cam/.stl. Файли відкриваються так, що Sum3D може їх переписувати й "
            "видаляти; теки робіт (CAM-work, export, диски) не обходяться; назв STL не віддає, "
            "літери поза латиницею затерто."
        ),
        schema={
            "type": "object",
            "properties": {
                "what": {"type": "string", "description": "roots (типово), map, files, schema, file, exe або assoc."},
                "root": {"type": "integer", "minimum": 1, "description": "Номер теки з what=roots (типово 1)."},
                "query": {"type": "string", "description": "Підрядок шляху (strateg, tool, material)."},
                "ext": {"type": "string", "description": "Розширення (xml, ini, str)."},
                "file": {"type": "integer", "minimum": 1, "description": "Для what=file: номер «№» з останнього переліку."},
                "start": {"type": "integer", "minimum": 0, "description": "Для what=file: з якого рядка."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 400, "description": "Скільки рядків або файлів."},
                "depth": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Для what=map: глибина підтек (типово 2)."},
            },
            "additionalProperties": False,
        },
        run=tool_sum3d_explore,
    ),
)
