"""Картки бота картинками, в одному стилі з CRM (власник 12.11.26).

Тут лише МАЛЮВАННЯ: кожна функція бере вже зібрані дані (dataclass-и нижче)
і повертає PNG. Дані збирає `telegram_cards`, тими самими предикатами, що
екрани CRM. Спільні елементи — `telegram_draw`.

«Пошта» повторює список «Нові з пошти» з CRM (варіант E): смуга матеріалів,
роздільник дня і по ОДНОМУ рядку на лист — крапка стану, ім'я, маркер
матеріалу, одиниці, файли, час, значок готовності. Теми й тексту листа на
картинці НЕМАЄ свідомо: у них бувають імена пацієнтів, а Telegram — зовнішній
сервіс (у текстових списках бота їх теж ніколи не було).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from app.services import telegram_draw as td
from app.services.telegram_draw import (
    ACCENT, GREEN, INK, INK_2, INK_3, LINE, ORANGE, PAD, PANEL, PANEL_HI, RED, ROW_LINE, TAB_ON, WIDTH,
    Canvas, Color, Span, header, mix, sub,
)

_W = WIDTH - 2 * PAD
_GAP = 16


def _stamp(now: datetime) -> str:
    return now.strftime("%d.%m · %H:%M")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# ── Пошта: список як у CRM ──────────────────────────────────────────────────

MAIL_PER_PAGE = 24
MAIL_TABS = (("zr", "Zr"), ("pm", "PMMA"), ("ti", "Ti"), ("wx", "Wax"), ("none", "без матер."))
_TAB_H, _DAY_H, _ROW_H, _FOOT_H = 76, 48, 58, 58
_DAY_INK: Color = (184, 168, 143)


def mail_pages(letters: Sequence, group: Optional[str]) -> int:
    shown = sum(1 for x in letters if group in (None, x.group))
    return max(1, math.ceil(shown / MAIL_PER_PAGE))


def _day_label(day, today) -> str:
    delta = (today - day).days
    if delta == 0:
        return "СЬОГОДНІ"
    if delta == 1:
        return "ВЧОРА"
    return ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "НД")[day.weekday()]


def _mail_tabs(c: Canvas, x0: float, y: float, counts: dict[str, int], total: int, group: Optional[str]) -> None:
    tabs = [(key, label) for key, label in MAIL_TABS if counts.get(key) or key in ("zr", "pm", "ti")]
    pad, disc_w = 20, 36

    def tab_width(label: str, count: int) -> float:
        return pad + disc_w + 10 + c.width(label, "m", 27) + 10 + c.width(str(count), "m", 24) + pad

    all_w = c.width("Усі", "x", 28) + 48
    if all_w + sum(tab_width(label, counts.get(key, 0)) for key, label in tabs) > _W:
        tabs = [(key, "інше" if key == "none" else label) for key, label in tabs]
    # «Усі»
    if group is None:
        c.d.rounded_rectangle([td.s(x0), td.s(y), td.s(x0 + all_w), td.s(y + _TAB_H)], radius=td.s(21),
                              fill=TAB_ON, corners=(True, False, False, False))
        c.box(x0, y + _TAB_H - 4, all_w, 4, fill=ACCENT)
    c.text(x0 + all_w / 2, y + _TAB_H / 2, "Усі", "x", 28, INK if group is None else INK_2, anchor="mm")
    x = x0 + all_w
    for key, label in tabs:
        count = counts.get(key, 0)
        color = td.MATERIALS[key][1] if key in td.MATERIALS else INK_2
        w = tab_width(label, count)
        c.line(x, y, x, y + _TAB_H, LINE)
        if group == key:
            c.box(x + 1, y, w - 1, _TAB_H, fill=mix(color, PANEL, 0.16))
            c.box(x + 1, y + _TAB_H - 4, w - 1, 4, fill=color)
        c.disc(x + pad, y + 23, disc_w, color, mix(color, PANEL, 0.16) if group == key else PANEL)
        cursor = x + pad + disc_w + 10
        cursor += c.text(cursor, y + 49, label, "m", 27, INK if key == "none" else color)
        c.text(cursor + 10, y + 48, str(count), "m", 24, INK_3 if count else mix(INK_3, PANEL, 0.5))
        x += w
    c.line(x, y, x, y + _TAB_H, LINE)
    c.line(x0, y + _TAB_H, x0 + _W, y + _TAB_H, LINE)


def _mail_row(c: Canvas, x0: float, y: float, letter) -> None:
    cy = y + _ROW_H / 2
    c.line(x0 + 1, y, x0 + _W - 1, y, ROW_LINE)
    if letter.unread:
        c.box(x0 + 1.5, y + 1, _W - 3, _ROW_H - 1, fill=PANEL_HI)
    if letter.group in ("pm", "ti", "wx"):
        c.box(x0 + 1.5, y + 1, 5, _ROW_H - 1, fill=td.MATERIALS[letter.group][1])
    x = x0 + 22
    c.dot(x + 5, cy, 5.5, GREEN if letter.ready else ORANGE)
    x += 22
    if letter.unread:
        c.dot(x + 5, cy, 5.5, ORANGE)
        x += 22
    badge_x = x0 + _W - 16 - 38
    time_right = badge_x - 16
    files_right = time_right - 92
    units_right = files_right - 166
    marker_w = 0.0
    if letter.group in td.MATERIALS:
        marker_w = 12 + c.width(td.MATERIALS[letter.group][0], "m", 23) + 11
        if letter.shade:
            marker_w += 9 + c.width(letter.shade, "m", 26)
    room = units_right - 104 - x - (marker_w + 14 if marker_w else 0) - 12
    name = c.fit(letter.name, "x", 29, room)
    x += c.text(x, cy + 10, name, "x", 29, INK)
    if marker_w:
        c.marker(x + 14, cy, letter.group, letter.shade)
    if letter.units:
        c.text(units_right, cy + 9, f"{letter.units} од.", "s", 26, INK, anchor="rs")
    else:
        c.text(units_right, cy + 9, "? од.", "s", 26, INK_3, anchor="rs")
    if letter.files_note:
        c.text(files_right, cy + 8, letter.files_note, "m", 22, ORANGE, anchor="rs")
    else:
        c.text(files_right, cy + 8, f"{letter.files} файл.", "m", 23, INK_2, anchor="rs")
    when = letter.received_at.strftime("%H:%M") if letter.received_at else "—"
    c.text(time_right, cy + 9, when, "m", 25, INK_2, anchor="rs")
    c.badge(badge_x, cy, letter.ready)


def mail_png(letters: Sequence, now: datetime, group: Optional[str] = None, page: int = 0) -> bytes:
    """Список «не взято в роботу». `letters` — найновіші зверху (`tv.mail_letters`)."""
    counts: dict[str, int] = {}
    for item in letters:
        counts[item.group] = counts.get(item.group, 0) + 1
    shown = [x for x in letters if group in (None, x.group)]
    pages = max(1, math.ceil(len(shown) / MAIL_PER_PAGE))
    page = max(0, min(page, pages - 1))
    part = shown[page * MAIL_PER_PAGE:(page + 1) * MAIL_PER_PAGE]
    days: list[tuple] = []
    for item in part:
        day = item.received_at.date() if item.received_at else None
        if not days or days[-1][0] != day:
            days.append((day, []))
        days[-1][1].append(item)
    body_h = sum(_DAY_H + len(rows) * _ROW_H for _, rows in days) if part else 250
    panel_h = _TAB_H + body_h + _FOOT_H
    c = Canvas(182 + panel_h + PAD)

    unread = sum(1 for x in letters if x.unread)
    units = sum(x.units or 0 for x in letters)
    parts: list[Span] = [sub("Вхідні "), sub(str(len(letters)), INK, "s")]
    if units:
        parts += [sub(" · "), sub(f"{units} од.", INK, "s")]
    if unread:
        parts += [sub(" · ")]
    top = header(c, "Нові з пошти", parts, _stamp(now))
    if unread:
        # Крапку малюємо, а не друкуємо: такого знака у шрифті немає.
        x = PAD + sum(c.width(text, kind, size) for text, kind, size, _ in parts)
        c.dot(x + 8, 139, 7, ORANGE)
        c.text(x + 24, 150, f"{unread} {_plural(unread, 'новий', 'нові', 'нових')}", "s", 30, ORANGE)

    c.box(PAD, top, _W, panel_h, fill=PANEL, outline=LINE, radius=22)
    _mail_tabs(c, PAD, top, counts, len(letters), group)
    y = top + _TAB_H
    if not part:
        cx = WIDTH / 2
        c.ring(cx, y + 96, 44, 6, 1.0, GREEN, track=LINE)
        c.check(cx, y + 98, 38, GREEN, 5)
        c.text(cx, y + 198, "Усі листи взято в роботу" if not letters else "У цій групі листів немає",
               "x", 34, INK, anchor="ms")
        y += 250
    for day, rows in days:
        label = _day_label(day, now.date()) if day else "БЕЗ ДАТИ"
        x = PAD + 22
        x += c.text(x, y + 34, label, "x", 22, _DAY_INK) + 14
        tail = f"{day.strftime('%d.%m')} · " if day else ""
        tail += f"{len(rows)} {_plural(len(rows), 'лист', 'листи', 'листів')}"
        x += c.text(x, y + 34, tail, "m", 22, INK_3) + 16
        c.line(x, y + 26, PAD + _W - 22, y + 26, LINE)
        y += _DAY_H
        for item in rows:
            _mail_row(c, PAD, y, item)
            y += _ROW_H
    # Підпис знизу: що означають значки, і яка сторінка.
    c.line(PAD + 1, y, PAD + _W - 1, y, ROW_LINE)
    fy = y + _FOOT_H / 2 + 8
    x = PAD + 22
    c.check(x + 9, fy - 9, 18, GREEN, 3)
    x += 28 + c.text(x + 28, fy, "готовий до прийняття", "t", 23, INK_3) + 26
    x += c.text(x, fy, "!", "x", 24, ORANGE) + 8
    x += c.text(x, fy, "перевірити", "t", 23, INK_3) + 26
    c.dot(x + 5, fy - 8, 5, ORANGE)
    c.dot(x + 19, fy - 8, 5, ORANGE)
    c.text(x + 32, fy, "ще не відкривали", "t", 23, INK_3)
    if pages > 1:
        first = page * MAIL_PER_PAGE + 1
        c.text(PAD + _W - 22, fy, f"{first}–{first + len(part) - 1} з {len(shown)}", "m", 23, INK_2, anchor="rs")
    return c.png()


# ── Головна: «Цех зараз» ────────────────────────────────────────────────────


@dataclass
class HomeTile:
    label: str
    value: str
    unit: str = ""
    note: list[tuple[str, Color]] = field(default_factory=list)
    tone: str = ""  # "" / "alarm" / "ok"
    value_color: Color = INK
    share: Optional[float] = None  # кільце праворуч
    ring_label: str = ""
    ring_color: Color = ACCENT
    photo: str = ""


_HOME_H = 226


def _home_tile(c: Canvas, tile: HomeTile, x: float, y: float, w: float) -> None:
    c.tile(x, y, w, _HOME_H, tone=tile.tone)
    room = w - 48
    if tile.photo:
        c.photo(tile.photo, x + w * 0.38, y + 2, w * 0.62 - 2, _HOME_H - 4, radius=24, opacity=0.8)
    if tile.share is not None:
        c.ring(x + w - 24 - 58, y + 24 + 58, 58, 13, tile.share, tile.ring_color, label=tile.ring_label,
               label_size=32)
        room -= 132
    c.text(x + 24, y + 52, tile.label, "s", 28, INK_2)
    size = 82 if len(tile.value) <= 4 else 58
    used = c.text(x + 24, y + 138, tile.value, "x", size, tile.value_color)
    if tile.unit:
        c.text(x + 24 + used + 12, y + 138, c.fit(tile.unit, "s", 29, room - used - 12), "s", 29, INK_2)
    cursor = x + 24
    for text, color in tile.note:
        text = c.fit(text, "s" if color != INK_2 else "t", 27, x + w - 24 - cursor)
        cursor += c.text(cursor, y + _HOME_H - 26, text, "s" if color != INK_2 else "t", 27, color)


def home_png(tiles: Sequence[HomeTile], now: datetime, banner: str = "", subtitle: str = "") -> bytes:
    rows = math.ceil(len(tiles) / 2) or 1
    banner_h = 72 if banner else 0
    c = Canvas(182 + banner_h + rows * (_HOME_H + _GAP) - _GAP + PAD)
    top = header(c, "Цех зараз", subtitle or "усе головне одним поглядом", _stamp(now))
    if banner:
        c.box(PAD, top, _W, 56, fill=(46, 28, 13), outline=(107, 61, 24), radius=16)
        c.text(PAD + 20, top + 38, c.fit(banner, "s", 26, _W - 40), "s", 26, ORANGE)
        top += banner_h
    half = (_W - _GAP) / 2
    for index, tile in enumerate(tiles):
        last_alone = index == len(tiles) - 1 and len(tiles) % 2 == 1
        x = PAD + (index % 2) * (half + _GAP)
        y = top + (index // 2) * (_HOME_H + _GAP)
        _home_tile(c, tile, x, y, _W if last_alone else half)
    return c.png()


# ── Печі ────────────────────────────────────────────────────────────────────


@dataclass
class FurnaceTile:
    name: str
    state: str  # run / idle / problem / unknown
    temp: Optional[int] = None
    remaining: str = ""
    opens: str = ""
    problem: str = ""


_FURN_H = 252


def furnaces_png(tiles: Sequence[FurnaceTile], now: datetime, subtitle: Sequence[Span] | str = "") -> bytes:
    rows = math.ceil(len(tiles) / 2) or 1
    empty = not tiles
    c = Canvas(182 + (220 if empty else rows * (_FURN_H + _GAP) - _GAP) + PAD)
    top = header(c, "Печі", subtitle or "табло печей спікання", _stamp(now))
    if empty:
        c.tile(PAD, top, _W, 200)
        c.text(WIDTH / 2, top + 112, "Даних із печей ще немає", "x", 34, INK_2, anchor="ms")
        return c.png()
    half = (_W - _GAP) / 2
    for index, tile in enumerate(tiles):
        last_alone = index == len(tiles) - 1 and len(tiles) % 2 == 1
        w = _W if last_alone else half
        x = PAD + (index % 2) * (half + _GAP)
        y = top + (index // 2) * (_FURN_H + _GAP)
        c.tile(x, y, w, _FURN_H, tone="alarm" if tile.state == "problem" else "")
        if tile.state == "run":
            c.photo("furnace-crowns.jpg", x + w * 0.3, y + 2, w * 0.7 - 2, _FURN_H - 4, radius=24,
                    opacity=0.95, fade_from=0.2)
        c.text(x + 24, y + 58, c.fit(tile.name, "x", 38, w - 48), "x", 38, INK)
        temp_color = ACCENT if tile.state == "run" else INK_2
        if tile.temp is not None:
            used = c.text(x + 24, y + 152, str(tile.temp), "x", 92, temp_color)
            c.text(x + 24 + used + 6, y + 152, "°C", "s", 38, INK_2)
        else:
            c.text(x + 24, y + 152, "—", "x", 92, INK_3)
        base = y + _FURN_H - 24
        if tile.state == "run":
            # Два рядки: в один «ще 7 год 45 хв · відкриється 13:35» не влазить.
            if tile.opens:
                c.spans(x + 24, base - 36, [("відкриється ", "t", 28, INK_2), (tile.opens, "x", 30, INK)])
            if tile.remaining:
                c.spans(x + 24, base, [("ще ", "t", 26, INK_2), (tile.remaining, "s", 26, INK_2)])
            if not (tile.opens or tile.remaining):
                c.text(x + 24, base, "працює", "s", 28, ACCENT)
        elif tile.state == "idle":
            c.text(x + 24, base, "вільна", "s", 28, GREEN)
        elif tile.state == "problem":
            c.text(x + 24, base, c.fit(tile.problem or "немає зв'язку", "t", 27, w - 48), "t", 27, RED)
        else:
            c.text(x + 24, base, "табло не читається", "t", 28, INK_3)
    return c.png()


# ── Лабораторія ─────────────────────────────────────────────────────────────


@dataclass
class LabRow:
    number: str
    group: str = ""
    shade: str = ""
    material: str = ""
    qty: str = ""
    tech: str = ""
    rush: bool = False


LAB_MAX_ROWS = 14
_LAB_ROW_H, _LAB_HEAD_H = 60, 72


def _lab_list(c: Canvas, y: float, title: str, rows: Sequence[LabRow], total: int, opened: bool,
              title_color: Color = INK) -> float:
    shown = list(rows[:LAB_MAX_ROWS]) if opened else []
    more = total - len(shown) if opened else 0
    h = _LAB_HEAD_H + len(shown) * _LAB_ROW_H + (_LAB_ROW_H if more > 0 else 0)
    c.box(PAD, y, _W, h, fill=PANEL, outline=LINE, radius=22)
    c.text(PAD + 22, y + 48, title, "x", 32, title_color)
    word = _plural(total, "робота", "роботи", "робіт")
    used = c.text(PAD + _W - 22, y + 47, f" {word}", "t", 27, INK_2, anchor="rs")
    c.text(PAD + _W - 22 - used, y + 48, str(total), "x", 34, INK, anchor="rs")
    cy = y + _LAB_HEAD_H
    for row in shown:
        c.line(PAD + 1, cy, PAD + _W - 1, cy, ROW_LINE)
        mid = cy + _LAB_ROW_H / 2
        x: float = PAD + 22
        c.text(x, mid + 10, row.number or "—", "m", 27, INK)
        x += 118
        if row.group in td.MATERIALS:
            x += c.marker(x, mid, row.group, row.shade) + 14
        elif row.material:
            x += c.text(x, mid + 9, c.fit(row.material, "m", 24, 260), "m", 24, INK_2) + 14
        if row.rush:
            w = c.width("на швидку", "s", 23) + 22
            c.box(x, mid - 18, w, 36, outline=mix(ACCENT, PANEL, 0.6), radius=10)
            c.text(x + 11, mid + 8, "на швидку", "s", 23, ACCENT)
            x += w + 14
        c.text(PAD + _W - 22, mid + 11, row.qty or "", "x", 32, INK, anchor="rs")
        room = PAD + _W - 22 - 90 - x
        c.text(PAD + _W - 22 - 90, mid + 9, c.fit(row.tech, "m", 24, room), "m", 24, INK_2, anchor="rs")
        cy += _LAB_ROW_H
    if more > 0:
        c.line(PAD + 1, cy, PAD + _W - 1, cy, ROW_LINE)
        c.text(PAD + 22, cy + 39, f"ще {more}", "t", 26, INK_3)
    return h


def lab_png(day_label: str, total: int, done: int, units_left: int, take: Sequence[LabRow],
            wait: Sequence[LabRow], now: datetime, *, opened_wait: bool = False,
            take_total: Optional[int] = None, wait_total: Optional[int] = None) -> bytes:
    take_total = len(take) if take_total is None else take_total
    wait_total = len(wait) if wait_total is None else wait_total

    def list_h(count: int, opened: bool) -> float:
        shown = min(count, LAB_MAX_ROWS) if opened else 0
        return _LAB_HEAD_H + shown * _LAB_ROW_H + (_LAB_ROW_H if opened and count > shown else 0)

    stat_h = 190
    height: float = 182 + stat_h
    if total and take_total:
        height += _GAP + list_h(take_total, True)
    if total and wait_total:
        height += _GAP + list_h(wait_total, opened_wait)
    c = Canvas(height + PAD)
    top = header(c, "Лабораторія", f"робочий день {day_label}", _stamp(now))
    c.tile(PAD, top, _W, stat_h, tone="ok" if total and done == total else "")
    if not total:
        c.text(WIDTH / 2, top + 108, "Лабораторних робіт немає", "x", 34, INK_2, anchor="ms")
        return c.png()
    share = done / total
    c.ring(PAD + 24 + 70, top + stat_h / 2, 70, 16, share, GREEN, label=f"{round(share * 100)}%", label_size=36)
    x = PAD + 24 + 140 + 28
    used = c.text(x, top + 104, str(done), "x", 80, INK)
    c.text(x + used + 10, top + 104, f"з {total}", "s", 38, INK_2)
    c.text(x, top + 148, "робіт прораховано" if done != total else "усе прораховано", "t", 28,
           INK_2 if done != total else GREEN)
    c.text(PAD + _W - 24, top + 104, str(units_left), "x", 80, ACCENT if units_left else INK_3, anchor="rs")
    c.text(PAD + _W - 24, top + 148, "одиниць лишилось", "t", 28, INK_2, anchor="rs")
    y = top + stat_h
    if take_total:
        y += _GAP
        y += _lab_list(c, y, "Можна брати", take, take_total, True)
    if wait_total:
        y += _GAP
        _lab_list(c, y, "Технік ще не здав", wait, wait_total, opened_wait, INK_2)
    return c.png()


# ── Видача ──────────────────────────────────────────────────────────────────


def handout_png(day_label: str, clients: int, clients_done: int, works: int, works_done: int,
                units: int, units_done: int, now: datetime) -> bytes:
    big_h, small_h = 250, 190
    c = Canvas(182 + big_h + (_GAP + small_h if works else 0) + PAD)
    top = header(c, "Видача", f"робочий день {day_label}", _stamp(now))
    all_done = bool(works) and works_done == works
    c.tile(PAD, top, _W, big_h, tone="ok" if all_done else "")
    if not works:
        c.text(WIDTH / 2, top + 138, "Клієнтських робіт немає", "x", 34, INK_2, anchor="ms")
        return c.png()
    c.photo("handout-tray.jpg", PAD + _W * 0.45, top + 2, _W * 0.55 - 2, big_h - 4, radius=24, opacity=0.55)
    share = clients_done / clients if clients else 0.0
    c.ring(PAD + 24 + 82, top + big_h / 2, 82, 18, share, GREEN, label=f"{round(share * 100)}%", label_size=42)
    x: float = PAD + 24 + 164 + 32
    used = c.text(x, top + 124, str(clients_done), "x", 96, INK)
    c.text(x + used + 12, top + 124, f"з {clients}", "s", 42, INK_2)
    c.text(x, top + 172, "клієнтів видано", "t", 30, INK_2)
    left = works - works_done
    c.text(x, top + 216, "усе видано" if all_done else f"ще чекає {left} {_plural(left, 'робота', 'роботи', 'робіт')}",
           "s", 28, GREEN if all_done else ACCENT)
    y = top + big_h + _GAP
    half = (_W - _GAP) / 2
    for index, (label, got, of) in enumerate((("Роботи", works_done, works), ("Одиниці", units_done, units))):
        x = PAD + index * (half + _GAP)
        c.tile(x, y, half, small_h)
        c.text(x + 24, y + 50, label, "s", 28, INK_2)
        used = c.text(x + 24, y + 130, str(got), "x", 76, INK)
        c.text(x + 24 + used + 10, y + 130, f"з {of}", "s", 34, INK_2)
        c.box(x + 24, y + small_h - 32, half - 48, 10, fill=LINE, radius=5)
        if of:
            c.box(x + 24, y + small_h - 32, max(10, (half - 48) * got / of), 10, fill=GREEN, radius=5)
    return c.png()


# ── Роботи за день ──────────────────────────────────────────────────────────


@dataclass
class MaterialBar:
    name: str
    group: str
    units: int
    works: int


@dataclass
class SourceBox:
    title: str
    works: int
    units: int
    can_take: int
    in_work: int
    not_ready: Optional[int] = None


def orders_png(day_label: str, works: int, units: int, materials: Sequence[MaterialBar],
               sources: Sequence[SourceBox], now: datetime) -> bytes:
    stat_h, bar_h, box_h = 170, 60, 214
    mat_h = (72 + len(materials) * bar_h + 14) if materials else 0
    height = 182 + stat_h + (_GAP + mat_h if mat_h else 0) + (_GAP + box_h if sources else 0)
    c = Canvas(height + PAD)
    top = header(c, "Роботи за день", f"робочий день {day_label}", _stamp(now))
    half = (_W - _GAP) / 2
    if not works:
        c.tile(PAD, top, _W, stat_h)
        c.text(WIDTH / 2, top + 98, "Робіт немає", "x", 34, INK_2, anchor="ms")
        return c.png()
    for index, (label, value, color) in enumerate((("Робіт", works, INK), ("Одиниць", units, ACCENT))):
        x = PAD + index * (half + _GAP)
        c.tile(x, top, half, stat_h)
        c.text(x + 24, top + 50, label, "s", 28, INK_2)
        c.text(x + 24, top + 136, str(value), "x", 84, color)
    y = top + stat_h
    if materials:
        y += _GAP
        c.box(PAD, y, _W, mat_h, fill=PANEL, outline=LINE, radius=22)
        c.text(PAD + 22, y + 48, "Матеріали", "x", 32, INK)
        peak = max(m.units for m in materials) or 1
        cy = y + 72
        for item in materials:
            color = td.MATERIALS[item.group][1] if item.group in td.MATERIALS else INK_3
            c.line(PAD + 1, cy, PAD + _W - 1, cy, ROW_LINE)
            mid = cy + bar_h / 2
            c.text(PAD + 22, mid + 10, c.fit(item.name, "s", 28, 200), "s", 28, INK)
            bar_x, bar_w = PAD + 240, _W - 240 - 270
            c.box(bar_x, mid - 9, bar_w, 18, fill=LINE, radius=9)
            c.box(bar_x, mid - 9, max(18, bar_w * item.units / peak), 18, fill=color, radius=9)
            tail = f" од. · {item.works} роб."
            used = c.text(PAD + _W - 22, mid + 9, tail, "t", 25, INK_2, anchor="rs")
            c.text(PAD + _W - 22 - used, mid + 10, str(item.units), "x", 30, INK, anchor="rs")
            cy += bar_h
        y += mat_h
    if sources:
        y += _GAP
        width = (_W - _GAP * (len(sources) - 1)) / len(sources)
        for index, box in enumerate(sources):
            x = PAD + index * (width + _GAP)
            c.tile(x, y, width, box_h)
            c.text(x + 24, y + 50, box.title, "s", 28, INK_2)
            used = c.text(x + 24, y + 116, str(box.works), "x", 60, INK)
            c.text(x + 24 + used + 10, y + 116, f"роб. · {box.units} од.", "s", 27, INK_2)
            lines: list[tuple[str, int, Color]] = [("можна брати", box.can_take, GREEN if box.can_take else INK_3),
                                                   ("в роботі", box.in_work, INK)]
            if box.not_ready is not None:
                lines.append(("не готово", box.not_ready, INK_2))
            # «можна брати» довше за сусідів — перша колонка ширша.
            for share, (label, value, color) in zip((0.0, 0.42, 0.72), lines):
                left = x + 24 + (width - 48) * share
                c.text(left, y + 166, str(value), "x", 32, color)
                c.text(left, y + 194, label, "t", 21, INK_2)
    return c.png()


# ── Sisma ───────────────────────────────────────────────────────────────────


@dataclass
class SismaTile:
    name: str
    state: str  # print / done / problem / idle
    layer: int = 0
    total: int = 0
    phase: str = ""
    started: str = ""
    ends: str = ""
    left: str = ""
    text: str = ""


def sisma_png(tiles: Sequence[SismaTile], now: datetime) -> bytes:
    tile_h = 264
    count = len(tiles) or 1
    c = Canvas(182 + count * (tile_h + _GAP) - _GAP + PAD)
    top = header(c, "Sisma", "SLM-принтер", _stamp(now))
    if not tiles:
        c.tile(PAD, top, _W, tile_h)
        c.text(WIDTH / 2, top + 142, "Принтер ще не впізнано", "x", 34, INK_2, anchor="ms")
        return c.png()
    for index, tile in enumerate(tiles):
        y = top + index * (tile_h + _GAP)
        tone = "alarm" if tile.state == "problem" else "ok" if tile.state == "done" else ""
        c.tile(PAD, y, _W, tile_h, tone=tone)
        x = PAD + 24
        if tile.state == "print" and tile.total:
            share = tile.layer / tile.total
            c.ring(x + 84, y + tile_h / 2, 84, 18, share, ACCENT, label=f"{round(share * 100)}%", label_size=44)
            x += 168 + 32
        elif tile.state == "done":
            c.ring(x + 84, y + tile_h / 2, 84, 18, 1.0, GREEN)
            c.check(x + 84, y + tile_h / 2 + 2, 64, GREEN, 8)
            x += 168 + 32
        c.text(x, y + 62, c.fit(tile.name, "x", 38, PAD + _W - 24 - x), "x", 38, INK)
        if tile.state == "print" and tile.total:
            used = c.text(x, y + 142, f"шар {tile.layer}", "x", 60, INK)
            c.text(x + used + 12, y + 142, f"з {tile.total}", "s", 34, INK_2)
            parts: list[Span] = []
            for label, value in (("старт ", tile.started), ("кінець ~ ", tile.ends)):
                if value:
                    parts += [((" · " if parts else "") + label, "t", 27, INK_2), (value, "s", 27, INK)]
            c.spans(x, y + 190, parts)
            tail = " · ".join(bit for bit in (tile.phase, tile.left) if bit)
            if tail:
                c.text(x, y + 232, c.fit(tail, "t", 27, PAD + _W - 24 - x), "t", 27, ACCENT)
        elif tile.state == "done":
            c.text(x, y + 142, "друк закінчився", "x", 52, GREEN)
        elif tile.state == "problem":
            c.text(x, y + 136, c.fit(tile.text or "немає зв'язку", "s", 34, PAD + _W - 24 - x), "s", 34, RED)
        else:
            c.text(x, y + 136, c.fit(tile.text or "даних немає", "s", 38, PAD + _W - 24 - x), "s", 38, INK_2)
    return c.png()
