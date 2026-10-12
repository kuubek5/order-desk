"""Найменший запис .xlsx без сторонніх бібліотек (zipfile + XML).

openpyxl/xlsxwriter у залежностях немає, а тягнути бібліотеку в інсталятор
заради кількох таблиць статистики — зайве. Формат простий: книга з аркушами,
рядки текстом (inlineStr) або числом, перший рядок — жирний заголовок.
Формул, дат-як-чисел і стилів понад жирний тут немає свідомо.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable, Sequence
from xml.sax.saxutils import escape

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")

Cell = str | int | float | None


def _col(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell(ref: str, value: Cell, bold: bool) -> str:
    style = ' s="1"' if bold else ""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = escape(_ILLEGAL.sub("", str(value)))
    return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(rows: Sequence[Sequence[Cell]]) -> str:
    widths: dict[int, int] = {}
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths.get(i, 0), len(str(value)) if value is not None else 0)
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{min(max(w + 2, 6), 60)}" customWidth="1"/>'
        for i, w in sorted(widths.items())
    )
    body = []
    for r, row in enumerate(rows, start=1):
        cells = "".join(_cell(f"{_col(c)}{r}", v, r == 1) for c, v in enumerate(row))
        body.append(f'<row r="{r}">{cells}</row>')
    cols_xml = f"<cols>{cols}</cols>" if cols else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN}">{cols_xml}<sheetData>{"".join(body)}</sheetData></worksheet>'
    )


def _sheet_name(name: str, used: set[str]) -> str:
    clean = _BAD_SHEET.sub(" ", name).strip()[:31] or "Аркуш"
    base, n = clean, 2
    while clean.casefold() in used:
        suffix = f" ({n})"
        clean = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(clean.casefold())
    return clean


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<styleSheet xmlns="{_MAIN}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def workbook(sheets: Iterable[tuple[str, Sequence[Sequence[Cell]]]]) -> bytes:
    """[(назва аркуша, рядки)] → байти .xlsx. Перший рядок кожного аркуша — заголовок."""
    items = list(sheets) or [("Аркуш", [])]
    used: set[str] = set()
    names = [_sheet_name(name, used) for name, _ in items]
    n = len(items)
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, n + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PKG_REL}"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    sheets_xml = "".join(
        f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(names, start=1)
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>{sheets_xml}</sheets></workbook>'
    )
    wb_rels = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, n + 1)
    )
    wb_rels += (
        f'<Relationship Id="rId{n + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_PKG_REL}">{wb_rels}</Relationships>',
        )
        zf.writestr("xl/styles.xml", _STYLES)
        for i, (_, rows) in enumerate(items, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows))
    return buf.getvalue()
