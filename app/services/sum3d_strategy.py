"""Читач файлів стратегій Sum3D (`*.cfg`, контейнер TECN 2.0). Лише читання.

Стратегія — це таблиця операцій і тіла операцій:

* запис таблиці: назва (154 або 254 байти, доповнена нулями), за нею заголовок
  24 байти — ``FF FF FF FF``, прапорець, байт типу обробки, службові 8 байт,
  зсув тіла, розмір тіла. Назва стоїть ПЕРЕД своїм заголовком: та, що йде
  після заголовка, належить наступній операції (поле 210 тіла «Machining name»
  це підтверджує; до 17.11.26 читач брав її й зсував усі назви на одну).
  Крок запису залежить від версії файлу (178 або 278 байт), тому він
  визначається з самого файлу — за ланцюжком «зсув + розмір = наступний зсув»;
* тіло операції — поля підряд у сталому порядку: текст, що закінчується
  байтом 0x0A, або число float32. На зсуві 64 кожного тіла стоїть текстове
  поле ``TECN 2.0`` — за ним знаходиться база, від якої рахуються зсуви.

Назва операції несе коди в кутових дужках (``<TS1,2,9>``, ``<T:/D1B-?/Roughing>``,
``<# Fissure fine /$3>``). Значення кодів тут не тлумачаться — лише розкладаються.

Номер поля у звітах дорівнює коду параметра виробника: мітка ``TECN 2.0`` — поле 0,
далі поля 1, 2, 3… Назви кодів лежать у встановленні Sum3D (`params_codes.txt`:
«19=Machining Type», «12=Oversize») і читаються звідти, а не зашиті тут.

Модуль нічого не пише й не запускає. Верхня частина (до «Файли») — чисті
функції над байтами без залежності від застосунку.
"""

from __future__ import annotations

import os
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

MACHINING_TYPES = (
    "Z-LEVEL PROFILING", "POCKETING", "ONE-WAY", "ZIG ZAG", "SPIRAL", "PENCIL MILLING",
    "2 CURVE MACHINING", "U-V LINES MACHINING", "REST MILLING", "3D PROFILE POCKETING",
    "FLAT SURF. MACHINING", "DRILLING", "CURVES MILLING", "WIRE EROSION", "*", "MULTI AXES", "3/4 AXES",
)
LABEL = b"TECN 2.0"
LABEL_AT = 64
HEADER_LEN = 24               # заголовок запису таблиці; назва — перед ним
SCAN_FROM = 0x2000
SCAN_BYTES = 6 * 1024 * 1024
MIN_BODY, MAX_BODY = 256, 64 * 1024
MAX_FILE = 80 * 1024 * 1024
STRATEGY_EXT = ".cfg"
# Назви файлів роботи (пацієнт буває в імені) у відповідь не йдуть.
_FILE_NAME = re.compile(r"\.(stl|cam|3dm|igs|iges|obj|ply|dcm|pts|constructioninfo|nc|iso)\b", re.IGNORECASE)
_SIGN = re.compile(rb"\xff\xff\xff\xff[\x00-\x0f][\x00-\x20]\x00\x00", re.DOTALL)
_CODE = re.compile(r"<([^<>]*)>")
_NUMERIC_TEXT = re.compile(rb"[0-9][0-9.,\-]{0,79}")
_LETTERS = re.compile(rb"[A-Za-z\xc0-\xff]")
_CODE_LINE = re.compile(r"^\s*(\d+)\s*=\s*(.*\S)\s*$")
_GROUP_LINE = re.compile(r"^\s*Group\s*:\s*(\d+)", re.IGNORECASE)
_ZERO = b"\x00\x00\x00\x00"
CODES_FILE = "params_codes.txt"
TOOL_NAME_CODE = 311          # текст «опис фрези/код» у тілі операції
Names = dict[int, tuple[str, frozenset[int]]]


class StrategyError(Exception):
    """Файл не розібрано; текст каже чому."""


@dataclass(frozen=True)
class Op:
    index: int          # 1-based, порядок у таблиці
    pos: int            # зсув запису таблиці у файлі
    flag: int
    kind: int           # байт типу обробки (1 = перший у MACHINING_TYPES)
    has_path: bool      # службове поле вказує на збережену траєкторію
    off: int
    size: int
    name: str


@dataclass
class Strategy:
    size: int
    stride: Optional[int]
    table_at: Optional[int]
    ops: list[Op]
    base: Optional[int] = None
    label_hits: int = 0
    label_checked: int = 0
    notes: list[str] = field(default_factory=list)

    def body(self, data: bytes, op: Op) -> Optional[bytes]:
        if self.base is None:
            return None
        start = self.base + op.off
        if start < 0 or start + op.size > len(data):
            return None
        return data[start:start + op.size]


def _printable(byte: int) -> bool:
    return 0x20 <= byte <= 0x7E or byte >= 0xA0


def _header(data: bytes, pos: int) -> Optional[tuple[int, int, int, int, bool]]:
    """(off, size, flag, kind, has_path) заголовка запису на `pos` або None."""
    if pos < 0 or pos + HEADER_LEN > len(data) or not _SIGN.match(data, pos):
        return None
    extra, _marker, off, size = struct.unpack_from("<IIII", data, pos + 8)
    if not MIN_BODY <= size <= MAX_BODY or off > len(data):
        return None
    return off, size, data[pos + 4], data[pos + 5], extra != 0


def _name_before(data: bytes, pos: int, name_len: int) -> Optional[str]:
    """Назва операції — у `name_len` байтах ПЕРЕД заголовком на `pos`."""
    if pos - name_len < 0:
        return None
    raw = data[pos - name_len:pos].split(b"\x00", 1)[0]
    if not raw or not all(_printable(b) for b in raw):
        return None
    return raw.decode("cp1252", "replace")


def parse(data: bytes) -> Strategy:
    """Таблиця операцій і база тіл. Не кидає на незнайомій будові — пише
    зауваги, щоб із відповіді було видно, що саме не зійшлось."""
    if data[:8] != LABEL:
        raise StrategyError("це не файл TECN 2.0 (стратегії Sum3D починаються з «TECN 2.0»)")
    found: list[tuple[int, int, int]] = []          # (pos, off, size)
    for match in _SIGN.finditer(data, SCAN_FROM, min(len(data), SCAN_FROM + SCAN_BYTES)):
        head = _header(data, match.start())
        if head:
            found.append((match.start(), head[0], head[1]))
    out = Strategy(size=len(data), stride=None, table_at=None, ops=[])
    if not found:
        out.notes.append("записів таблиці операцій не знайдено")
        return out
    gaps = Counter(
        b[0] - a[0] for a, b in zip(found, found[1:])
        if 64 <= b[0] - a[0] <= 1024 and a[1] + a[2] == b[1]
    )
    stride = gaps.most_common(1)[0][0] if gaps else None
    out.stride = stride
    name_len = (stride - HEADER_LEN) if stride else 254
    if stride is None:
        positions = [f[0] for f in found if _name_before(data, f[0], name_len)]
        if len(found) > 1:
            out.notes.append("крок запису не визначено: записи не йдуть ланцюжком")
    else:
        start = next(a[0] for a, b in zip(found, found[1:]) if b[0] - a[0] == stride and a[1] + a[2] == b[1])
        positions = []
        pos = start
        while _header(data, pos) and _name_before(data, pos, name_len):
            positions.append(pos)
            pos += stride
        skipped = sum(1 for f in found if f[0] < start)
        if skipped:
            out.notes.append(f"перед таблицею пропущено схожих записів: {skipped}")
    if not positions:
        out.notes.append("записів таблиці операцій не знайдено")
        return out
    out.table_at = positions[0] - name_len
    for number, pos in enumerate(positions, start=1):
        head = _header(data, pos)
        name = _name_before(data, pos, name_len)
        if head and name:
            off, size, flag, kind, has_path = head
            out.ops.append(Op(number, pos, flag, kind, has_path, off, size, name))
    _find_base(data, out)
    return out


def _find_base(data: bytes, out: Strategy) -> None:
    """База тіл: мітка `TECN 2.0` стоїть на зсуві 64 кожного тіла, тож
    відстані між мітками мають збігатися з розмірами тіл."""
    ordered = sorted({op.off for op in out.ops})
    table_end = max(op.pos for op in out.ops) + HEADER_LEN
    labels: list[int] = []
    for match in re.finditer(re.escape(LABEL), data[table_end:]):
        labels.append(table_end + match.start())
        if len(labels) >= 4000:
            break
    if not labels:
        out.notes.append("міток тіл операцій не знайдено")
        return
    want = [o - ordered[0] for o in ordered[1:6]]
    for j in range(min(len(labels), 200)):
        if all(j + k + 1 < len(labels) and labels[j + k + 1] - labels[j] == w for k, w in enumerate(want)):
            out.base = labels[j] - LABEL_AT - ordered[0]
            break
    if out.base is None:
        out.notes.append("база тіл не знайдена: відстані між мітками не збігаються з розмірами тіл")
        return
    sample = out.ops[:: max(1, len(out.ops) // 300)]
    out.label_checked = len(sample)
    out.label_hits = sum(
        1 for op in sample
        if data[out.base + op.off + LABEL_AT:out.base + op.off + LABEL_AT + len(LABEL)] == LABEL
    )
    if len(want) == 0:
        out.notes.append("одна операція: база взята за першою міткою після таблиці, не перевірена ланцюжком")


# ── Назва операції ───────────────────────────────────────────────────────


def describe_name(name: str) -> dict[str, Any]:
    """Розкласти назву на коди. Нічого не тлумачить."""
    codes = _CODE.findall(name)
    plain = " ".join(_CODE.sub(" ", name).split())
    info: dict[str, Any] = {"текст": plain}
    other: list[str] = []
    for code in codes:
        head = code.strip()
        upper = head.upper()
        types = re.fullmatch(r"(?:TS|TYPESEL)\s*(-?\d+(?:\s*,\s*-?\d+)*)", upper)
        if types:
            numbers = [int(x) for x in types.group(1).replace(" ", "").split(",")]
            if any(n < 0 for n in numbers):
                info.setdefault("крім_типів", []).extend(sorted(-n for n in numbers if n < 0))
            if any(n >= 0 for n in numbers):
                info.setdefault("типи", []).extend(n for n in numbers if n >= 0)
        elif upper.startswith("T:/"):
            parts = head[3:].split("/")
            info["фреза"] = parts[0]
            if len(parts) > 1:
                info["набір"] = parts[1]
        elif head.startswith("#"):
            text, _, group = head[1:].partition("/")
            info["опція"] = text.strip()
            group = group.strip("/$ ")
            if group:
                info["група_опції"] = group
        else:
            other.append(head)
    if other:
        info["коди"] = other
    return info


# ── Поля тіла ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Field:
    at: int
    text: Optional[str] = None
    number: Optional[float] = None
    raw: bytes = b""

    @property
    def key(self) -> tuple[str, Any]:
        return ("t", self.text) if self.text is not None else ("n", self.raw)


def _text_at(body: bytes, pos: int) -> Optional[int]:
    """Кінець текстового поля (індекс байта 0x0A), якщо на `pos` текст."""
    end = pos
    limit = min(len(body), pos + 2048)
    while end < limit and _printable(body[end]):
        end += 1
    if end == pos or end >= len(body) or body[end] != 0x0A:
        return None
    run = body[pos:end]
    if not (_LETTERS.search(run) or _NUMERIC_TEXT.fullmatch(run)):
        return None
    # «fff?» — це 0.9, а не текст: кругле число з правдоподібною величиною
    # читаємо числом, навіть коли всі чотири байти друковані.
    if len(run) >= 4 and _round_number(run[:4]):
        return None
    return end


def _round_number(raw: bytes) -> bool:
    value = struct.unpack("<f", raw)[0]
    if value != value or not 1e-4 <= abs(value) <= 1e6:
        return False
    # Кругле = записується чотирма значущими цифрами без втрати жодного біта.
    return struct.pack("<f", float(f"{value:.4g}")) == raw


def fields(body: bytes) -> tuple[list[Field], int]:
    """(поля, зайвих байтів у кінці). Порожній текст — один байт 0x0A.

    Перші 64 байти тіла — службовий заголовок, не поля: розбір іде від
    мітки `TECN 2.0`, яка сама є першим текстовим полем."""
    out: list[Field] = []
    pos = LABEL_AT if body[LABEL_AT:LABEL_AT + len(LABEL)] == LABEL else 0
    size = len(body)
    while pos < size:
        # 0.08 записується як 0A D7 A3 3D: байт кінця рядка буває й початком числа.
        if body[pos] == 0x0A and not (pos + 4 <= size and _round_number(body[pos:pos + 4])):
            out.append(Field(pos, text=""))
            pos += 1
            continue
        end = _text_at(body, pos) if body[pos] != 0x0A else None
        if end is not None:
            out.append(Field(pos, text=body[pos:end].decode("cp1252", "replace")))
            pos = end + 1
            continue
        if pos + 4 > size:
            break
        raw = body[pos:pos + 4]
        out.append(Field(pos, number=struct.unpack("<f", raw)[0], raw=raw))
        pos += 4
    return out, size - pos


def _number_view(item: Field) -> Any:
    value = item.number
    if value is None or value != value:      # NaN
        return "0x" + item.raw.hex()
    if value == 0:
        return 0
    if 1e-5 <= abs(value) < 1e7:
        return round(value, 5)
    return "0x" + item.raw.hex()


def _text_view(text: str, mask: Any) -> str:
    if _FILE_NAME.search(text):
        ext = _FILE_NAME.search(text)
        return f"‹назва файлу .{ext.group(1).lower()}›" if ext else "‹назва файлу›"
    return mask(text)[:200]


def parse_codes(text: str) -> Names:
    """Назви полів від виробника (`params_codes.txt`): код → (назва, групи).

    Група — номер типу обробки (1 Z-LEVEL PROFILING, 2 POCKETING…), 0 — спільні
    для всіх типів параметри."""
    found: dict[int, tuple[str, set[int]]] = {}
    group = 0
    for line in text.splitlines():
        hit = _GROUP_LINE.match(line)
        if hit:
            group = int(hit.group(1))
            continue
        hit = _CODE_LINE.match(line)
        if hit:
            found.setdefault(int(hit.group(1)), (hit.group(2), set()))[1].add(group)
    return {code: (name, frozenset(groups)) for code, (name, groups) in found.items()}


def _first_code(items: list[Field]) -> int:
    """Код першого поля: мітка `TECN 2.0` — це поле 0, без неї рахунок з 1."""
    return 0 if items and items[0].text == LABEL.decode() else 1


def field_text(body: bytes, code: int) -> Optional[str]:
    """Текст поля з кодом `code` або None (поля немає чи воно число)."""
    items, _tail = fields(body)
    index = code - _first_code(items)
    return items[index].text if 0 <= index < len(items) else None


def field_rows(items: list[Field], mask: Any, names: Optional[Names] = None, *,
               kind: int = 0, own: bool = False) -> list[dict[str, Any]]:
    """Поля для відповіді; підряд нульові числа без назви згорнуто в один рядок.

    `own` лишає тільки поля з назвою, що стосуються цієї операції: спільні
    (група 0) і свого типу обробки."""
    names = names or {}
    first = _first_code(items)
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(items):
        item = items[i]
        code = first + i
        named = names.get(code)
        if own and not (named and (0 in named[1] or kind in named[1])):
            i += 1
            continue
        if not named and item.text is None and item.raw == _ZERO:
            j = i
            while j < len(items) and items[j].text is None and items[j].raw == _ZERO and first + j not in names:
                j += 1
            if j - i >= 3:
                rows.append({"№": code, "нулів_підряд": j - i})
                i = j
                continue
        row: dict[str, Any] = {"№": code}
        if named:
            row["назва"] = named[0]
        row["текст" if item.text is not None else "число"] = _show(item, mask)
        rows.append(row)
        i += 1
    return rows


def _show(item: Field, mask: Any) -> Any:
    return _text_view(item.text, mask) if item.text is not None else _number_view(item)


def compare_bodies(a: bytes, b: bytes, mask: Any, limit: int = 60,
                   names: Optional[Names] = None) -> dict[str, Any]:
    """Різниця двох тіл по полях."""
    fa, tail_a = fields(a)
    fb, tail_b = fields(b)
    names = names or {}
    first = _first_code(fa)
    out: dict[str, Any] = {"полів": [len(fa), len(fb)]}
    if tail_a or tail_b:
        out["зайвих_байтів"] = [tail_a, tail_b]
    changes: list[dict[str, Any]] = []
    if len(fa) == len(fb):
        for i, (x, y) in enumerate(zip(fa, fb)):
            if x.key != y.key:
                change: dict[str, Any] = {"№": first + i}
                if first + i in names:
                    change["назва"] = names[first + i][0]
                changes.append({**change, "було": _show(x, mask), "стало": _show(y, mask)})
    else:
        matcher = SequenceMatcher(a=[f.key for f in fa], b=[f.key for f in fb], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            changes.append({
                "№": first + i1, "дія": {"replace": "замінено", "delete": "прибрано", "insert": "додано"}[tag],
                "було": [_show(f, mask) for f in fa[i1:i2][:8]],
                "стало": [_show(f, mask) for f in fb[j1:j2][:8]],
            })
    out["відмінностей"] = len(changes)
    out["відмінності"] = changes[:limit]
    return out


# ── Звіти ────────────────────────────────────────────────────────────────


def _kind_name(kind: int) -> str:
    return MACHINING_TYPES[kind - 1] if 1 <= kind <= len(MACHINING_TYPES) else f"тип {kind}"


def layout_report(strategy: Strategy) -> dict[str, Any]:
    report: dict[str, Any] = {
        "крок_запису": strategy.stride, "таблиця_з": strategy.table_at, "операцій": len(strategy.ops),
        "база_тіл": strategy.base,
    }
    if strategy.label_checked:
        report["мітка_тіла_на_місці"] = f"{strategy.label_hits} з {strategy.label_checked}"
    if strategy.notes:
        report["зауваги"] = strategy.notes
    return report


def ops_report(data: bytes, strategy: Strategy, mask: Any, *, start: int = 0, limit: int = 120) -> dict[str, Any]:
    rows = []
    for op in strategy.ops[start:start + limit]:
        row: dict[str, Any] = {"№": op.index, "назва": mask(op.name), "тип_обробки": _kind_name(op.kind),
                               "тіло_байт": op.size}
        info = describe_name(op.name)
        for key in ("типи", "крім_типів", "фреза", "набір", "опція", "група_опції", "коди"):
            if key in info:
                row[key] = mask(info[key]) if isinstance(info[key], str) else info[key]
        if op.has_path:
            row["є_траєкторія"] = True
        rows.append(row)
    kinds = Counter(_kind_name(op.kind) for op in strategy.ops)
    return {
        "будова": layout_report(strategy), "типи_обробки": dict(kinds.most_common()),
        "з": start, "операції": rows,
    }


def op_report(data: bytes, strategy: Strategy, number: int, mask: Any, *,
              start: int = 0, limit: int = 200, names: Optional[Names] = None,
              own: bool = False) -> dict[str, Any]:
    if not 1 <= number <= len(strategy.ops):
        raise StrategyError(f"операції №{number} немає (1…{len(strategy.ops)})")
    op = strategy.ops[number - 1]
    body = strategy.body(data, op)
    base: dict[str, Any] = {"№": op.index, "назва": mask(op.name), "тип_обробки": _kind_name(op.kind),
                            "розклад_назви": describe_name(mask(op.name)), "тіло_байт": op.size}
    if body is None:
        return {**base, "поля": "тіло не знайдено: база тіл не визначена", "будова": layout_report(strategy)}
    items, tail = fields(body)
    rows = field_rows(items, mask, names, kind=op.kind, own=own)
    return {**base, "полів": len(items), "зайвих_байтів_у_кінці": tail, "з": start,
            "рядків_усього": len(rows), "поля": rows[start:start + limit]}


def _plain(name: str) -> str:
    return " ".join(_CODE.sub(" ", name).split()).lower()


def diff_report(data_a: bytes, a: Strategy, data_b: bytes, b: Strategy, mask: Any, *,
                op: Optional[int] = None, limit: int = 40, names: Optional[Names] = None) -> dict[str, Any]:
    """Чим стратегія B відрізняється від A: операції за назвою, далі тіла."""
    by_name_b: dict[str, list[Op]] = {}
    for item in b.ops:
        by_name_b.setdefault(item.name, []).append(item)
    pairs: list[tuple[Op, Op]] = []
    only_a: list[Op] = []
    used: set[int] = set()
    for item in a.ops:
        twins = [t for t in by_name_b.get(item.name, []) if t.index not in used]
        if twins:
            used.add(twins[0].index)
            pairs.append((item, twins[0]))
        else:
            only_a.append(item)
    only_b = [item for item in b.ops if item.index not in used]
    renamed: list[tuple[Op, Op]] = []
    plain_b: dict[str, list[Op]] = {}
    for item in only_b:
        plain_b.setdefault(_plain(item.name), []).append(item)
    for item in list(only_a):
        twins = plain_b.get(_plain(item.name)) or []
        if twins:
            twin = twins.pop(0)
            renamed.append((item, twin))
            only_a.remove(item)
            only_b.remove(twin)
    if op is not None:
        pair = next((p for p in pairs + renamed if p[0].index == op), None)
        if pair is None:
            raise StrategyError(f"операції №{op} з першого файлу немає в другому за назвою")
        body_a, body_b = a.body(data_a, pair[0]), b.body(data_b, pair[1])
        if body_a is None or body_b is None:
            raise StrategyError("тіло операції не знайдено: база тіл не визначена в одному з файлів")
        return {"операція": mask(pair[0].name), "у_другому": mask(pair[1].name),
                **compare_bodies(body_a, body_b, mask, limit=max(limit, 200), names=names)}
    changed: list[dict[str, Any]] = []
    same = 0
    for left, right in pairs + renamed:
        body_a, body_b = a.body(data_a, left), b.body(data_b, right)
        if body_a is not None and body_a == body_b and left.name == right.name:
            same += 1
            continue
        row: dict[str, Any] = {"№": left.index, "назва": mask(left.name)}
        if left.name != right.name:
            row["назва_в_другому"] = mask(right.name)
        if left.kind != right.kind:
            row["тип_обробки"] = [_kind_name(left.kind), _kind_name(right.kind)]
        if body_a is not None and body_b is not None and body_a != body_b:
            detail = compare_bodies(body_a, body_b, mask, limit=6, names=names)
            row["відмінностей_у_полях"] = detail["відмінностей"]
            if detail["відмінностей"]:
                row["перші"] = detail["відмінності"]
            else:
                row["де"] = "поза полями: службовий заголовок тіла або зайві байти в кінці"
        changed.append(row)
    return {
        "будова_першого": layout_report(a), "будова_другого": layout_report(b),
        "однакових": same, "змінених": len(changed),
        "лише_в_першому": [{"№": o.index, "назва": mask(o.name)} for o in only_a][:limit],
        "лише_в_другому": [{"№": o.index, "назва": mask(o.name)} for o in only_b][:limit],
        "змінені": changed[:limit],
    }


# ── Файли ────────────────────────────────────────────────────────────────


def load(path: str) -> tuple[bytes, Strategy]:
    """Прочитати файл стратегії спільним доступом (Sum3D може його переписати)."""
    from app.shared_read import read_shared

    if os.path.splitext(path)[1].lower() != STRATEGY_EXT:
        raise StrategyError("розбираються лише файли стратегій .cfg")
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE:
            raise StrategyError(f"файл завеликий: {size // 1024**2} МБ (межа {MAX_FILE // 1024**2} МБ)")
        data = read_shared(path, MAX_FILE)
    except OSError as exc:
        raise StrategyError(f"файл не прочитано: {exc.__class__.__name__}") from None
    return data, parse(data)


def find_up(path: str, name: str, levels: int = 6) -> Optional[str]:
    """Файл `name` у теці файлу `path` або вище (не далі `levels` рівнів)."""
    folder = os.path.dirname(os.path.abspath(path))
    for _step in range(levels):
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def load_codes(path: str) -> Names:
    """Назви полів із `params_codes.txt` встановлення, якому належить стратегія.

    Файла немає або він не читається — порожній словник: звіт іде без назв."""
    from app.shared_read import read_shared

    found = find_up(path, CODES_FILE)
    if not found:
        return {}
    try:
        return parse_codes(read_shared(found, 1024 * 1024).decode("cp1252", "replace"))
    except OSError:
        return {}
