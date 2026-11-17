"""Архів фрез Sum3D (`Tools.arl`): чим і як ріже кожна фреза. Лише читання.

Стратегія нового стилю не тримає обертів і подач у собі: операція називає
фрезу й набір параметрів (``<T:/D2B-?/Roughing>``), а числа лежать тут.
Файл текстовий, перший рядок ``R4SUM_TOOL``:

    <TOOLSTART>
      Machine=…, ToolGroup=<матеріал>, ToolType, Diameter, ToolNumber,
      Description=<опис>/<код>, StrategyCode=<код фрези для стратегії>
      <TOOLSTARTPARMAT>
        MaterialName=<набір параметрів>, Enabled,
        RotationSpeed, FeedXY, FeedZ, MillMaxStepXY, MillMaxStepZ, …
      <TOOLENDPARMAT>
    <TOOLEND>

Ключі віддаються як у файлі — без перекладу й тлумачення. Модуль нічого не
пише й не запускає; верхня частина (до «Файли») — чисті функції над текстом.
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

SIGN = "R4SUM_TOOL"
ARCHIVE_FILE = "Tools.arl"
MAX_FILE = 80 * 1024 * 1024
# Форма за ToolType: виведено з архівів (F — пласка, B — куля, T — тор, Angle — конічна).
SHAPES = {"1": "пласка", "2": "куля", "3": "тор", "5": "конічна", "21": "фасонна"}
_SET_KEYS = ("RotationSpeed", "FeedXY", "FeedZ", "FeedXYCut", "ApproachAngleZ", "MillMaxStepXY",
             "MillMaxStepZ", "OverMetal", "CorrectionRadius", "OptionalCodes")


class ToolsError(Exception):
    """Файл не є архівом фрез або його не вдалося прочитати."""


@dataclass
class Cutter:
    index: int                                   # 1-й, 2-й… у файлі
    fields: dict[str, str] = field(default_factory=dict)
    sets: list[dict[str, str]] = field(default_factory=list)

    def get(self, key: str) -> str:
        return self.fields.get(key, "")

    @property
    def code(self) -> str:
        """Код фрези для стратегії: `StrategyCode`, у старих записах — хвіст опису після «/»."""
        code = self.get("StrategyCode")
        if code:
            return code
        description = self.get("Description")
        return description.rsplit("/", 1)[1].strip() if "/" in description else ""


def parse(text: str) -> list[Cutter]:
    lines = text.splitlines()
    if not lines or not lines[0].startswith(SIGN):
        raise ToolsError("це не архів фрез: файл не починається з R4SUM_TOOL")
    out: list[Cutter] = []
    cutter: Optional[Cutter] = None
    params: Optional[dict[str, str]] = None
    for raw in lines[1:]:
        line = raw.strip()
        if line == "<TOOLSTART>":
            cutter, params = Cutter(len(out) + 1), None
        elif line == "<TOOLEND>":
            if cutter is not None:
                out.append(cutter)
            cutter, params = None, None
        elif line == "<TOOLSTARTPARMAT>":
            params = {}
        elif line == "<TOOLENDPARMAT>":
            if cutter is not None and params is not None:
                cutter.sets.append(params)
            params = None
        elif "=" in line and cutter is not None:
            key, value = line.split("=", 1)
            (params if params is not None else cutter.fields)[key.strip()] = value.strip()
    return out


def _value(text: str, mask: Any) -> Any:
    try:
        number = float(text)
    except ValueError:
        return mask(text)[:200]
    if number != number or abs(number) > 1e15:
        return mask(text)[:200]
    return int(number) if number == int(number) and "." not in text else number


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _row(cutter: Cutter, mask: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"№": cutter.index, "верстат": mask(cutter.get("Machine")),
                           "група": mask(cutter.get("ToolGroup")), "код": mask(cutter.code)}
    shape = cutter.get("ToolType")
    if shape:
        row["форма"] = SHAPES.get(shape, f"тип {shape}")
    for key, title in (("Diameter", "діаметр"), ("Diameter2", "діаметр2"), ("Radius", "радіус"),
                       ("Length", "довжина"), ("ToolNumber", "гніздо")):
        if cutter.get(key):
            row[title] = _value(cutter.get(key), mask)
    row["опис"] = mask(cutter.get("Description"))[:120]
    return row


def _set_row(params: dict[str, str], mask: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"набір": mask(params.get("MaterialName", ""))}
    if params.get("Enabled") == "0":
        row["вимкнено"] = True
    for key, value in params.items():
        if key not in ("MaterialName", "Enabled"):
            row[key] = _value(value, mask)
    return row


def _keyed_sets(cutter: Cutter) -> list[tuple[tuple[str, int], dict[str, str]]]:
    """Набори фрези з ключем (назва, котрий з однойменних).

    В одній фрезі буває кілька наборів з однаковою назвою й різними числами;
    котрий із них бере Sum3D — невідомо, тому жоден не відкидається."""
    seen: Counter[str] = Counter()
    out = []
    for params in cutter.sets:
        name = params.get("MaterialName", "").strip().lower()
        seen[name] += 1
        out.append(((name, seen[name]), params))
    return out


def machines(cutters: list[Cutter]) -> list[str]:
    return sorted({c.get("Machine") for c in cutters if c.get("Machine")})


def _filtered(cutters: list[Cutter], machine: str, group: str, query: str = "") -> list[Cutter]:
    machine, group, query = machine.strip().lower(), group.strip().lower(), query.strip().lower()
    out = []
    for cutter in cutters:
        if machine and machine not in cutter.get("Machine").lower():
            continue
        if group and group != cutter.get("ToolGroup").lower():
            continue
        if query and not any(query in text.lower() for text in (
                cutter.code, cutter.get("Description"), cutter.get("ToolProducer"))):
            continue
        out.append(cutter)
    return out


def tools_report(cutters: list[Cutter], mask: Any, *, machine: str = "", group: str = "",
                 query: str = "", start: int = 0, limit: int = 120) -> dict[str, Any]:
    """Перелік фрез з відбором за верстатом, групою (матеріалом) і текстом."""
    found = _filtered(cutters, machine, group, query)
    rows = []
    for cutter in found[start:start + limit]:
        row = _row(cutter, mask)
        row["наборів"] = len(cutter.sets)
        disabled = sum(1 for params in cutter.sets if params.get("Enabled") == "0")
        if disabled:
            row["вимкнених"] = disabled
        rows.append(row)
    by_machine = Counter(c.get("Machine") for c in cutters)
    by_group = Counter(c.get("ToolGroup") for c in _filtered(cutters, machine, "", ""))
    return {
        "фрез_у_файлі": len(cutters), "верстати": {mask(k): v for k, v in by_machine.most_common()},
        "групи": {mask(k): v for k, v in by_group.most_common()},
        "знайдено": len(found), "з": start, "фрези": rows,
    }


def tool_report(cutters: list[Cutter], number: int, mask: Any) -> dict[str, Any]:
    """Одна фреза: усі її поля й усі набори параметрів."""
    if not 1 <= number <= len(cutters):
        raise ToolsError(f"фрези №{number} немає (1…{len(cutters)})")
    cutter = cutters[number - 1]
    return {
        **_row(cutter, mask),
        "поля": {key: _value(value, mask) for key, value in cutter.fields.items()},
        "набори": [_set_row(params, mask) for params in cutter.sets],
    }


def link_report(cutters: list[Cutter], mask: Any, *, code: str = "", set_name: str = "",
                description: str = "", machine: str = "", group: str = "", limit: int = 20) -> dict[str, Any]:
    """Чим різатиме операція: фрези з кодом `code` (або описом) і їхній набір `set_name`.

    Набір у назві операції буває з дописом після коми (``Anatomical_Restmill,OPT``) —
    шукається і повна назва, і частина до коми. Без назви набору віддаються всі набори."""
    wanted = [name for name in {set_name.strip(), set_name.split(",")[0].strip()} if name]
    matched = []
    for cutter in _filtered(cutters, machine, group):
        if code and _same(cutter.code, code):
            matched.append(cutter)
        elif not code and description and _same(cutter.get("Description"), description):
            matched.append(cutter)
    rows: list[dict[str, Any]] = []
    without = 0
    twins = False
    for cutter in matched:
        keyed = [(key, p) for key, p in _keyed_sets(cutter)
                 if not wanted or any(key[0] == w.lower() for w in wanted)]
        if not keyed:
            without += 1
            continue
        total = Counter(key[0] for key, _p in keyed)
        for (name, nth), params in keyed:
            row = {**_row(cutter, mask), **_set_row(params, mask)}
            if total[name] > 1:
                row["однойменний"] = f"{nth} з {total[name]}"
                twins = True
            rows.append(row)
    out: dict[str, Any] = {"фрез_з_таким_кодом": len(matched), "рядків": len(rows), "рядки": rows[:limit]}
    if without:
        out["фрез_без_цього_набору"] = without
    if twins:
        out["увага"] = "у фрези кілька наборів з однаковою назвою; котрий бере Sum3D — невідомо"
    if len(rows) > limit:
        out["обрізано"] = True
    return out


def _changes(a: dict[str, str], b: dict[str, str], mask: Any, skip: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    out = []
    for key in list(a) + [k for k in b if k not in a]:
        if key in skip or a.get(key) == b.get(key):
            continue
        out.append({"ключ": key, "було": _value(a[key], mask) if key in a else None,
                    "стало": _value(b[key], mask) if key in b else None})
    return out


def diff_report(a: list[Cutter], b: list[Cutter], mask: Any, *, machine: str = "", group: str = "",
                limit: int = 60) -> dict[str, Any]:
    """Чим архів B відрізняється від A: фрези за `ID`, набори за назвою."""
    a, b = _filtered(a, machine, group), _filtered(b, machine, group)
    by_id_b: dict[str, Cutter] = {}
    for cutter in b:
        by_id_b.setdefault(cutter.get("ID"), cutter)
    seen: set[str] = set()
    removed: list[Cutter] = []
    changed: list[dict[str, Any]] = []
    for cutter in a:
        ident = cutter.get("ID")
        twin = by_id_b.get(ident)
        if twin is None or ident in seen:
            removed.append(cutter)
            continue
        seen.add(ident)
        fields_changed = _changes(cutter.fields, twin.fields, mask)
        sets_b = dict(_keyed_sets(twin))
        sets_changed: list[dict[str, Any]] = []
        keys_a = set()
        for key, params in _keyed_sets(cutter):
            name = mask(params.get("MaterialName", ""))
            keys_a.add(key)
            other = sets_b.get(key)
            if other is None:
                sets_changed.append({"набір": name, "дія": "прибрано"})
                continue
            for change in _changes(params, other, mask, skip=("MaterialName",)):
                sets_changed.append({"набір": name, **change})
        for key, params in _keyed_sets(twin):
            if key not in keys_a:
                sets_changed.append({"набір": mask(params.get("MaterialName", "")), "дія": "додано"})
        if fields_changed or sets_changed:
            row = _row(cutter, mask)
            if fields_changed:
                row["поля"] = fields_changed[:12]
            if sets_changed:
                row["змін_у_наборах"] = len(sets_changed)
                row["набори"] = sets_changed[:12]
            changed.append(row)
    added = [cutter for cutter in b if cutter.get("ID") not in seen]
    return {
        "фрез": [len(a), len(b)], "змінено": len(changed), "додано": len(added), "прибрано": len(removed),
        "додані": [_row(c, mask) for c in added[:limit]], "прибрані": [_row(c, mask) for c in removed[:limit]],
        "змінені": changed[:limit],
    }


# ── Файли ────────────────────────────────────────────────────────────────

_cache: dict[tuple[str, int, int], list[Cutter]] = {}
_cache_lock = threading.Lock()


def is_archive_name(path: str) -> bool:
    """`Tools.arl` і його датовані копії (`Tools.arl.250908122720`)."""
    name = os.path.basename(path).lower()
    return name.endswith(".arl") or ".arl." in name


def load(path: str) -> list[Cutter]:
    """Прочитати архів фрез спільним доступом (Sum3D може його переписати)."""
    from app.shared_read import read_shared

    if not is_archive_name(path):
        raise ToolsError("архів фрез — це файл .arl (Tools.arl або його датована копія)")
    try:
        stat = os.stat(path)
        if stat.st_size > MAX_FILE:
            raise ToolsError(f"файл завеликий: {stat.st_size // 1024**2} МБ (межа {MAX_FILE // 1024**2} МБ)")
        key = (os.path.abspath(path), stat.st_mtime_ns, stat.st_size)
        with _cache_lock:
            if key in _cache:
                return _cache[key]
        cutters = parse(read_shared(path, MAX_FILE).decode("cp1252", "replace"))
    except OSError as exc:
        raise ToolsError(f"файл не прочитано: {exc.__class__.__name__}") from None
    with _cache_lock:
        if len(_cache) >= 3:
            _cache.clear()
        _cache[key] = cutters
    return cutters
