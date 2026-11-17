"""Інструмент MCP: глибока розвідка Sum3D (`sum3d_explore`). Лише читання."""

from __future__ import annotations

import os
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


def tool_sum3d_layout(db: Session, args: dict) -> dict[str, Any]:
    from app.services import sum3d_layout

    return sum3d_layout.report(_int_arg(args, "limit", default=60, low=1, high=400))


def tool_sum3d_probe(db: Session, args: dict) -> dict[str, Any]:
    from app.services import sum3d_probe_report

    return sum3d_probe_report.report(_int_arg(args, "limit", default=3, low=1, high=20))


_STRATEGY_WHAT = ("ops", "op", "diff", "tools", "tool", "tooldiff")


def _text_arg(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()[:120]


def _strategy_tools(what: str, path: str, args: dict, mask: Any) -> dict[str, Any]:
    """Архів фрез (`Tools.arl`): перелік, одна фреза, різниця двох архівів."""
    from app.services import sum3d_tools

    cutters = sum3d_tools.load(path)
    base = {"файл": mask(os.path.basename(path)), "фрез_у_файлі": len(cutters)}
    machine, group = _text_arg(args, "machine"), _text_arg(args, "group")
    if what == "tools":
        return {**base, **sum3d_tools.tools_report(
            cutters, mask, machine=machine, group=group, query=_text_arg(args, "query"),
            start=_int_arg(args, "start", default=0, low=0, high=1_000_000),
            limit=_int_arg(args, "limit", default=120, low=1, high=400))}
    if what == "tool":
        if args.get("tool") in (None, ""):
            raise ToolError("для `tool` потрібен `tool` — номер фрези з what=tools")
        return {**base, **sum3d_tools.tool_report(cutters, _int_arg(args, "tool", default=0, low=1, high=1_000_000), mask)}
    if args.get("other") in (None, ""):
        raise ToolError("для `tooldiff` потрібен `other` — номер «№» другого архіву з того самого переліку")
    other_path = sum3d_explore.listed_path(_int_arg(args, "other", default=0, low=1, high=100_000))
    return {**base, "другий_файл": mask(os.path.basename(other_path)),
            **sum3d_tools.diff_report(cutters, sum3d_tools.load(other_path), mask, machine=machine, group=group,
                                      limit=_int_arg(args, "limit", default=60, low=1, high=400))}


def _op_cutters(path: str, data: bytes, strategy: Any, number: int, args: dict, mask: Any) -> dict[str, Any]:
    """Чим ріже операція: рядки архіву фрез для її фрези й набору параметрів."""
    from app.services import sum3d_strategy, sum3d_tools

    archive = sum3d_strategy.find_up(path, sum3d_tools.ARCHIVE_FILE)
    if not archive:
        return {"архів_фрез": "Tools.arl поруч зі стратегією й вище не знайдено"}
    try:
        cutters = sum3d_tools.load(archive)
    except sum3d_tools.ToolsError as exc:
        return {"архів_фрез": str(exc)}
    op = strategy.ops[number - 1]
    info = sum3d_strategy.describe_name(op.name)
    body = strategy.body(data, op)
    described = sum3d_strategy.field_text(body, sum3d_strategy.TOOL_NAME_CODE) if body is not None else None
    if not info.get("фреза") and not described:
        return {"архів_фрез": "операція не називає фрезу"}
    machine = _text_arg(args, "machine")
    if not machine:
        # Тека стратегії зазвичай названа верстатом — беремо його фрези.
        folder = os.path.dirname(path).lower()
        named = [m for m in sum3d_tools.machines(cutters) if m.lower() in folder]
        machine = max(named, key=len) if named else ""
    link = sum3d_tools.link_report(
        cutters, mask, code=str(info.get("фреза") or ""), set_name=str(info.get("набір") or ""),
        description=described or "", machine=machine, group=_text_arg(args, "group"))
    return {"архів_фрез": {"верстат": mask(machine) or "усі", "за": "кодом фрези" if info.get("фреза") else "описом фрези",
                           **link}}


def tool_sum3d_strategy(db: Session, args: dict) -> dict[str, Any]:
    from app.services import sum3d_strategy, sum3d_tools
    from app.services.machine_files import mask

    what = str(args.get("what") or "ops").strip().lower()
    if what not in _STRATEGY_WHAT:
        raise ToolError("`what` — " + ", ".join(_STRATEGY_WHAT) + " (типово ops)")
    if args.get("file") in (None, ""):
        raise ToolError("потрібен `file` — номер «№» з останнього переліку kmill_sum3d_explore (what=files)")
    start = _int_arg(args, "start", default=0, low=0, high=1_000_000)
    try:
        path = sum3d_explore.listed_path(_int_arg(args, "file", default=0, low=1, high=100_000))
        if what in ("tools", "tool", "tooldiff"):
            return _strategy_tools(what, path, args, mask)
        data, strategy = sum3d_strategy.load(path)
        names = sum3d_strategy.load_codes(path)
        base = {"файл": mask(os.path.basename(path)), "байт": len(data),
                "назви_полів": "з params_codes.txt" if names else "немає (params_codes.txt не знайдено)"}
        if what == "ops":
            return {**base, **sum3d_strategy.ops_report(
                data, strategy, mask, start=start, limit=_int_arg(args, "limit", default=120, low=1, high=400))}
        if what == "op":
            if args.get("op") in (None, ""):
                raise ToolError("для `op` потрібен `op` — номер операції з what=ops")
            number = _int_arg(args, "op", default=0, low=1, high=100_000)
            report = sum3d_strategy.op_report(
                data, strategy, number, mask, start=start, names=names, own=bool(args.get("own")),
                limit=_int_arg(args, "limit", default=200, low=1, high=400))
            return {**base, **report, **_op_cutters(path, data, strategy, number, args, mask)}
        if args.get("other") in (None, ""):
            raise ToolError("для `diff` потрібен `other` — номер «№» другого файлу з того самого переліку")
        other_path = sum3d_explore.listed_path(_int_arg(args, "other", default=0, low=1, high=100_000))
        other_data, other = sum3d_strategy.load(other_path)
        op = _int_arg(args, "op", default=0, low=1, high=100_000) if args.get("op") not in (None, "") else None
        return {**base, "другий_файл": mask(os.path.basename(other_path)), "другий_байт": len(other_data),
                **sum3d_strategy.diff_report(data, strategy, other_data, other, mask, op=op, names=names,
                                             limit=_int_arg(args, "limit", default=40, low=1, high=400))}
    except (sum3d_explore.ExploreError, sum3d_strategy.StrategyError, sum3d_tools.ToolsError) as exc:
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
    Tool(
        name="kmill_sum3d_layout",
        description=(
            "Розкладка іконок Sum3D (панелі інструментів у CAMWINC.INI кожного встановлення з "
            "Налаштувань): які розкладки бували і скільки разів, яка зараз, і події — коли розкладка "
            "змінилась, при якій роздільності, після закриття якої Sum3D (з якої теки) і скільки Sum3D "
            "було відкрито водночас; запуски й закриття camwinc.exe. Щоб знайти, після чого іконки "
            "«злітають». Назв проєктів не віддає. Лише читання."
        ),
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 400, "description": "Скільки останніх подій (типово 60)."},
            },
            "additionalProperties": False,
        },
        run=tool_sum3d_layout,
    ),
    Tool(
        name="kmill_sum3d_probe",
        description=(
            "Звіти набору розвідки Sum3D (tools/sum3d-probe), який власник запускає РУКАМИ на ПК з "
            "ліцензією: чи відповідає API Sum3dDll_x64.dll (кроки load, init, machines, strategy, "
            "space, curves з кодами й значеннями), які теки Sum3D знайдено, ліміт сесій, що "
            "змінилось у теці й реєстрі Sum3D за час проби, чи ті самі вікна. Також де лежить набір. "
            "KuubMill набір не запускає — інструмент лише читає готові report.json; шляху й назви "
            "проєкту .cam у звіті немає, літери поза латиницею затерто."
        ),
        schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Скільки останніх звітів (типово 3)."},
            },
            "additionalProperties": False,
        },
        run=tool_sum3d_probe,
    ),
    Tool(
        name="kmill_sum3d_strategy",
        description=(
            "Стратегія Sum3D і архів фрез до неї, лише читання. Спершу kmill_sum3d_explore "
            "what=files — номер «№» файлу береться з того переліку. Файл стратегії (.cfg, формат "
            "TECN 2.0): what=ops (типово; file, start, limit) — будова файлу й таблиця операцій: "
            "назва, тип обробки, розклад кодів назви (типи робіт, фреза, набір параметрів, опція "
            "оператора, інші коди). what=op (file, op, start, limit, own) — поля тіла однієї операції; "
            "номер поля = код параметра виробника, назва поля — з params_codes.txt встановлення; "
            "own=true лишає тільки названі поля свого типу обробки. Разом з op — «архів_фрез»: оберти, "
            "подачі й кроки з Tools.arl для фрези й набору цієї операції (machine, group — відбір; "
            "верстат типово з назви теки стратегії). what=diff (file, other) — чим другий файл "
            "відрізняється від першого; з op — усі відмінності полів однієї операції. Архів фрез "
            "(Tools.arl і його датовані копії): what=tools (file, machine, group, query, start, limit) "
            "— фрези: верстат, група-матеріал, код, форма, діаметр, гніздо, скільки наборів. what=tool "
            "(file, tool) — одна фреза з усіма наборами параметрів. what=tooldiff (file, other, machine, "
            "group) — що змінилось між двома архівами: фрези, поля, числа в наборах. Значень не "
            "тлумачить. Назви файлів робіт (.stl, .cam тощо) не віддає, літери поза латиницею затерто."
        ),
        schema={
            "type": "object",
            "properties": {
                "what": {"type": "string", "description": "ops (типово), op, diff — стратегія .cfg; tools, tool, tooldiff — архів фрез .arl."},
                "file": {"type": "integer", "minimum": 1, "description": "Номер «№» файлу з останнього переліку kmill_sum3d_explore what=files."},
                "other": {"type": "integer", "minimum": 1, "description": "Для diff і tooldiff: номер «№» другого файлу з того самого переліку."},
                "op": {"type": "integer", "minimum": 1, "description": "Номер операції з what=ops (для op; для diff — показати поля однієї операції)."},
                "own": {"type": "boolean", "description": "Для op: лише названі поля, спільні й свого типу обробки."},
                "tool": {"type": "integer", "minimum": 1, "description": "Для tool: номер фрези з what=tools."},
                "machine": {"type": "string", "description": "Відбір фрез за верстатом (частина назви)."},
                "group": {"type": "string", "description": "Відбір фрез за групою-матеріалом (ZR, PMMA, WAX…), точна назва."},
                "query": {"type": "string", "description": "Для tools: текст у коді, описі чи виробнику фрези."},
                "start": {"type": "integer", "minimum": 0, "description": "З якої операції (ops), рядка полів (op) або фрези (tools)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 400, "description": "Скільки рядків."},
            },
            "required": ["file"],
            "additionalProperties": False,
        },
        run=tool_sum3d_strategy,
    ),
)
