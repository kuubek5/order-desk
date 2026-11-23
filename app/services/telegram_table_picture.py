"""Малювання «Таблиці дня» (варіант C, власник 23.11.26). Дані — `telegram_table`."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from app.services import telegram_draw as td
from app.services.telegram_draw import (
    ACCENT, GREEN, INK, INK_2, INK_3, LINE, PAD, PANEL, RED, ROW_LINE, WIDTH, Canvas, Color, header, mix, sub,
)
from app.services.telegram_table import DayTable, TableRow, table_summary

_W = WIDTH - 2 * PAD
_HEAD_H, _LINE_H, _WRAP_H = 52, 46, 34
#: Ліва межа колонки від краю таблиці; у двох останніх — права.
_ROW, _NO, _MAT, _KIND, _TECH, _QTY, _ID = 16, 74, 186, 340, 628, 806, _W - 16
#: «Технік не здав» — тьмяний рядок.
_DIM: Color = (139, 128, 109)


def _row(c: Canvas, y: float, row: TableRow) -> None:
    take, wait = row.state == "take", row.state == "wait"
    ink = _DIM if wait else INK
    if take:
        c.d.rectangle([td.s(PAD + 1), td.s(y), td.s(PAD + _W - 1), td.s(y + _LINE_H)], fill=mix(ACCENT, PANEL, 0.13))
    c.line(PAD + 1, y, PAD + _W - 1, y, ROW_LINE)
    edge = {"done": GREEN, "take": ACCENT}.get(row.state)
    base = y + _LINE_H / 2 + 9
    if edge:
        c.d.rectangle([td.s(PAD + 1), td.s(y + 1), td.s(PAD + 6), td.s(y + _LINE_H)], fill=edge)
    c.text(PAD + _ROW, base - 1, str(row.sheet_row or "—"), "m", 21, INK_3 if not wait else mix(INK_3, PANEL, 0.8))
    c.text(PAD + _NO, base, c.fit(row.number or "—", "m", 25, _MAT - _NO - 10), "m", 25, ink)
    x: float = PAD + _MAT
    room = _KIND - _MAT - 10
    if row.group in td.MATERIALS:
        tag, color, _back = td.MATERIALS[row.group]
        used = c.text(x, base - 1, tag, "m", 22, mix(color, PANEL, 0.75) if wait else color) + 8
        c.text(x + used, base, c.fit(row.shade, "s", 25, room - used), "s", 25, ink)
    else:
        c.text(x, base, c.fit(row.material, "t", 25, room), "t", 25, ink)
    kind = c.fit(row.kind, "t", 25, _TECH - _KIND - (34 if row.rush else 14))
    used = c.text(PAD + _KIND, base, kind, "t", 25, ink)
    if row.rush:
        c.text(PAD + _KIND + used + 8, base, "!", "x", 28, ACCENT)
    c.text(PAD + _TECH, base, c.fit(row.tech, "t", 25, _QTY - _TECH - 46), "t", 25, ink)
    c.text(PAD + _QTY, base, c.fit(row.quantity or "?", "x", 27, 60), "x", 27, ink, anchor="rs")
    if row.sum3d:
        c.text(PAD + _ID, base, c.fit(row.sum3d, "m", 22, _ID - _QTY - 12), "m", 22, ink, anchor="rs")
    elif take:
        c.text(PAD + _ID, base, "брати", "x", 25, ACCENT, anchor="rs")


_REWORK = "Переробка"
#: Довша причина ріжеться трикрапкою: картка — огляд дня, не паспорт роботи.
_REASON_MAX_LINES = 3


def _wrap(c: Canvas, text: str) -> list[str]:
    """Причина по рядках: її мусить бути видно цілком (власник 23.11.26)."""
    room = _W - _NO - 16
    lines: list[str] = []
    current = ""
    lead = c.width(_REWORK, "x", 24)
    for word in f"· {text}".split(" "):
        trial = f"{current} {word}" if current else word
        if current and c.width(f" {trial}", "t", 25) > room - (0 if lines else lead):
            lines.append(current)
            trial = word
        current = trial
    lines.append(current)
    if len(lines) > _REASON_MAX_LINES:
        lines = [*lines[:_REASON_MAX_LINES - 1], " ".join(lines[_REASON_MAX_LINES - 1:])]
    return lines


def _rework_lines(c: Canvas, y: float, lines: Sequence[str]) -> float:
    h = _LINE_H + (len(lines) - 1) * _WRAP_H
    c.d.rectangle([td.s(PAD + 1), td.s(y), td.s(PAD + 6), td.s(y + h)], fill=RED)
    base = y + _LINE_H / 2 + 7
    used = c.text(PAD + _NO, base, _REWORK, "x", 24, RED)
    for index, line in enumerate(lines):
        x = used if index == 0 else 0
        text = c.fit(f" {line}" if index == 0 else line, "t", 25, _W - _NO - x - 16)
        c.text(PAD + _NO + x, base + index * _WRAP_H, text, "t", 25, RED)
    return h


def table_png(day_label: str, table: DayTable, rows: Sequence[TableRow], page: int, pages: int, now: datetime) -> bytes:
    ruler = Canvas(1)
    reasons = {id(row): _wrap(ruler, row.rework.text) for row in rows if row.rework}
    body = _HEAD_H + len(rows) * _LINE_H + sum(_LINE_H + (len(lines) - 1) * _WRAP_H for lines in reasons.values())
    height = 182 + (body + 66 if rows else 190)
    c = Canvas(height + PAD)
    stamp = now.strftime("%d.%m · %H:%M")
    title = "Таблиця дня" if pages == 1 else f"Таблиця дня {page + 1}/{pages}"
    top = header(c, title, [sub(f"{day_label} · {table_summary(table)}" if table.rows
                                else f"лабораторія {day_label}")], stamp)
    if not rows:
        c.tile(PAD, top, _W, 190)
        c.text(WIDTH / 2, top + 108, "Лабораторних робіт немає", "x", 34, INK_2, anchor="ms")
        return c.png()
    c.box(PAD, top, _W, body, fill=PANEL, outline=LINE, radius=22)
    for x, text in ((_ROW, "ряд."), (_NO, "наряд"), (_MAT, "колір"), (_KIND, "вид"), (_TECH, "технік")):
        c.text(PAD + x, top + 35, text, "s", 22, INK_3)
    for x, text in ((_QTY, "од."), (_ID, "Sum3D")):
        c.text(PAD + x, top + 35, text, "s", 22, INK_3, anchor="rs")
    y = top + _HEAD_H
    for row in rows:
        _row(c, y, row)
        y += _LINE_H
        if id(row) in reasons:
            y += _rework_lines(c, y, reasons[id(row)])
    legend = [("зелений край", GREEN), (": прораховано · ", INK_2), ("жовтий", ACCENT),
              (": можна брати · тьмяний: технік не здав", INK_2)]
    c.spans(PAD + 4, y + 46, [(text, "t", 25, color) for text, color in legend])
    return c.png()
