"""Картинка «📬 Пошта» для Telegram-бота (власник 10.11.26, варіант E).

Два великі числа (листів чекають / одиниць), смуга на матеріал (довжина =
одиниці), унизу найдовше очікування й «не відкрито N». Список листів сюди не
влазить: під картинкою кнопка «Листи списком» відкриває текстовий вид.

Даних модуль не збирає: усе приходить готовим `MailBoard` з
`telegram_views.mail_board` — тими самими предикатами, що текстовий вид і
рядок у головному меню. Малювання й шрифти — ті самі, що в картинці
верстатів (`telegram_board`): подвійний розмір і зменшення, системні шрифти
Windows, символи лініями, а не гліфами (емодзі Pillow не малює).
"""

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw

from app.services.telegram_board import (
    _BG,
    _BLUSH,
    _GREEN,
    _GRID,
    _INK,
    _INK_2,
    _PAD,
    WIDTH,
    _fit,
    _font,
    _line,
    _s,
)
from app.services.telegram_board import _SCALE as SCALE
from app.services.telegram_views import MailBoard, letters_word, wait_text

_TRACK = (34, 39, 45)
_ROW_INK = (233, 230, 228)
_VALUE_INK = (201, 198, 196)
_WAIT_INK = (240, 165, 154)
_RULE = (38, 44, 51)

_HEAD = 168
_BIG = 150
_ROW_H = 58
_BAR_H = 34
_LABEL_W = 270
_VALUE_W = 250


def _hatch(width: int, height: int, radius: int) -> Image.Image:
    """Штрихована смуга «одиниць не вказано»: діагоналі в заокругленій рамці."""
    tile = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    step, stroke = _s(20), _s(8)
    for x in range(-height, width + height, step):
        draw.line([(x, height), (x + height, 0)], fill=(*_BLUSH, 150), width=stroke)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=255)
    tile.putalpha(ImageChops.multiply(tile.getchannel("A"), mask))
    return tile


def _bar_value(row) -> str:
    units = f"{row.units} од." if row.units_known else "? од."
    return f"{row.letters} л · {units}"


def render_png(board: MailBoard, now: datetime) -> bytes:
    rows = board.rows or []
    body = (len(rows) * _ROW_H) if rows else 70
    has_foot = board.waited is not None or bool(board.unread)
    height = _HEAD + 24 + _BIG + 70 + body + (30 + 96 if has_foot else 0) + _PAD
    image = Image.new("RGB", (_s(WIDTH), _s(height)), _BG)
    draw = ImageDraw.Draw(image)
    for gx in range(0, WIDTH + 1, 32):
        draw.line([(_s(gx), 0), (_s(gx), _s(height))], fill=_GRID, width=SCALE)
    for gy in range(0, height + 1, 32):
        draw.line([(0, _s(gy)), (_s(WIDTH), _s(gy))], fill=_GRID, width=SCALE)

    # Шапка — як у картинці верстатів: назва, час праворуч, підрядок.
    draw.text((_s(_PAD), _s(26)), "Пошта", font=_font("bold", 58), fill=_INK)
    stamp = now.strftime("%d.%m · %H:%M")
    stamp_font = _font("mono", 34)
    draw.text((_s(WIDTH - _PAD) - draw.textlength(stamp, font=stamp_font), _s(46)), stamp,
              font=stamp_font, fill=_INK_2)
    draw.text((_s(_PAD), _s(110)), "не взято в роботу · без 3D-друку, моделювання й СЛМ",
              font=_font("text", 34), fill=_INK_2)

    # Два великі числа з підписами.
    big_font, label_font = _font("bold", _BIG), _font("semi", 36)
    top = _HEAD + 24
    first = str(board.letters)
    first_label = letters_word(board.letters)
    draw.text((_s(_PAD), _s(top)), first, font=big_font, fill=_INK, anchor="lt")
    draw.text((_s(_PAD + 4), _s(top + _BIG - 8)), first_label, font=label_font, fill=_INK_2, anchor="lt")
    first_w = max(draw.textlength(first, font=big_font), draw.textlength(first_label, font=label_font)) / SCALE
    second_x = _PAD + first_w + 72
    second = str(board.units)
    draw.text((_s(second_x), _s(top)), second, font=big_font, fill=_BLUSH, anchor="lt")
    draw.text((_s(second_x + 4), _s(top + _BIG - 8)), "одиниць", font=label_font, fill=_INK_2, anchor="lt")

    # Смуги матеріалів: довжина — частка одиниць від найбільшої групи.
    y = top + _BIG + 70
    track_x = _PAD + _LABEL_W
    track_w = WIDTH - 2 * _PAD - _LABEL_W - _VALUE_W - 24
    row_font, value_font = _font("text", 36), _font("semi", 34)
    if not rows:
        _line(draw, [(_PAD + 4, y + 2), (_PAD + 18, y + 16), (_PAD + 42, y - 12)], _GREEN, 8)
        draw.text((_s(_PAD + 62), _s(y + 2)), "Усі листи взято в роботу", font=_font("semi", 40),
                  fill=_GREEN, anchor="lm")
        y += 70
    top_units = max([r.units for r in rows if r.units_known] or [0])
    for row in rows:
        mid = y + _ROW_H / 2 - 8
        draw.text((_s(_PAD), _s(mid)), _fit(draw, row.title, row_font, _LABEL_W - 16),
                  font=row_font, fill=_ROW_INK, anchor="lm")
        bar_top = mid - _BAR_H / 2
        draw.rounded_rectangle([_s(track_x), _s(bar_top), _s(track_x + track_w), _s(bar_top + _BAR_H)],
                               radius=_s(10), fill=_TRACK)
        if row.units_known and top_units:
            fill_w = max(24, track_w * row.units / top_units)
            draw.rounded_rectangle([_s(track_x), _s(bar_top), _s(track_x + fill_w), _s(bar_top + _BAR_H)],
                                   radius=_s(10), fill=_BLUSH)
        else:
            hatch = _hatch(_s(max(48, track_w * 0.12)), _s(_BAR_H), _s(10))
            image.paste(hatch, (_s(track_x), _s(bar_top)), hatch)
        value = _bar_value(row)
        draw.text((_s(WIDTH - _PAD), _s(mid)), value, font=value_font, fill=_VALUE_INK, anchor="rm")
        y += _ROW_H

    # Низ: роздільник, найдовше очікування, «не відкрито N».
    y += 30
    if has_foot:
        draw.line([(_s(_PAD), _s(y)), (_s(WIDTH - _PAD), _s(y))], fill=_RULE, width=_s(2))
    foot = y + 50
    foot_font, foot_bold = _font("text", 36), _font("bold", 36)
    if board.waited is not None:
        lead = "найдовше чекає "
        draw.text((_s(_PAD), _s(foot)), lead, font=foot_font, fill=_INK_2, anchor="lm")
        x = _PAD + draw.textlength(lead, font=foot_font) / SCALE
        draw.text((_s(x), _s(foot)), wait_text(board.waited), font=foot_bold, fill=_WAIT_INK, anchor="lm")
    if board.unread:
        pill = f"не відкрито {board.unread}"
        pill_font = _font("semi", 32)
        pw = draw.textlength(pill, font=pill_font) / SCALE + 44
        left = WIDTH - _PAD - pw
        draw.rounded_rectangle([_s(left), _s(foot - 26), _s(WIDTH - _PAD), _s(foot + 26)], radius=_s(26),
                               outline=(150, 124, 115), width=_s(2))
        draw.text((_s(left + 22), _s(foot)), pill, font=pill_font, fill=_BLUSH, anchor="lm")

    final = image.resize((WIDTH, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    final.save(out, format="PNG", optimize=True)
    return out.getvalue()
