"""Картинка-табло верстатів для Telegram-бота (власник 08.11.26, варіант C).

Текстове меню «⚙️ Верстати» було списком «назва — %», і що вимагає людини,
доводилось вичитувати. Тепер бот шле зображення: плитка на верстат, кільце
відсотка, колір стану, чіп матеріалу. Під ним підпис і кнопки верстатів.

Даних модуль не збирає: верстат приходить з `shop_board.board_machine`, тим
самим збирачем, що малює телевізор. Друге джерело правди про той самий
верстат заводити не можна (CLAUDE.md §14, табло цеху).

Малюється в подвійному розмірі й зменшується: Pillow не згладжує дуги й
лінії, а після зменшення краї рівні. Шрифти — системні шрифти Windows
(прод, CI і dev — Windows); без них Pillow бере свій запасний шрифт, і
картинка гірша, але бот не падає. Символи стану (✓, ✕, ▸) малюються лініями,
а не гліфами: гліфа може не бути в шрифті, і замість галочки вийде квадрат.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# Ширина готової картинки. На телефоні фото займає ~330 pt, тобто кожен
# піксель тут ≈ 0.3 pt: шрифт 34 px читається як 11 pt. Розміри нижче
# підібрані під це, а не під повнорозмірний перегляд.
WIDTH = 1080
_SCALE = 2  # малюємо вдвічі більше й зменшуємо — згладжування

_PAD = 32
_GAP = 16
_HEAD = 176
_TILE_H = 214
_RADIUS = 22

_BG = (16, 19, 23)
_GRID = (23, 27, 32)
_TILE = (21, 25, 30)
_TILE_EDGE = (38, 44, 51)
_INK = (236, 240, 243)
_INK_2 = (146, 157, 168)
_INK_MUTED = (176, 185, 193)
_BLUSH = (242, 198, 182)
_GREEN = (121, 209, 153)
_RED = (239, 138, 126)
_AMBER = (236, 190, 112)
_CHIP = (35, 42, 49)
_CHIP_EDGE = (52, 60, 69)
_CHIP_INK = (230, 214, 201)
_RING_TRACK = (42, 49, 56)

# Відсоток, від якого кільце зеленіє: «скоро знімати».
NEAR_DONE = 85

# Тон плитки за `state_key`. Невідомий новий ключ читається як спокійний,
# а не як «фрезерує» — те саме правило, що на телевізорі.
_ALARM = {"off", "fault"}
_QUIET = {"idle", "busy", "check", "wait", "unreadable"}

_DATE_TAIL = re.compile(r"[_-]?\d{4}-\d{2}-\d{2}[_-]?$")


@dataclass
class Tile:
    """Одна плитка — усе вже готове до малювання."""

    key: str
    name: str
    state: str
    percent: Optional[int] = None
    material: str = ""
    line: str = ""       # нижній рядок: чия робота або пояснення стану
    lead: str = ""       # слово перед рядком іншим кольором («зняти»)


@dataclass
class Board:
    tiles: list[Tile] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.tiles)

    @property
    def running(self) -> int:
        return sum(1 for t in self.tiles if t.state == "run")

    @property
    def done(self) -> int:
        return sum(1 for t in self.tiles if t.state == "done")

    @property
    def alarms(self) -> int:
        return sum(1 for t in self.tiles if t.state in _ALARM)


# ── Дані ────────────────────────────────────────────────────────────────────


def _first_material(card, last: bool) -> str:
    """Матеріал першої роботи (за id, як на табло). Окремо від рядка робіт
    табло: там матеріал склеєний з кількістю й видом, і на чіп він не лягає."""
    orders = list(getattr(card, "last_orders" if last else "orders", None) or [])
    orders.sort(key=lambda o: (getattr(o, "id", 0) or 0))
    for order in orders:
        value = (getattr(order, "material_color", "") or "").strip()
        if value:
            return value.upper()
    return ""


def _program_short(head: str, tail: str) -> str:
    """«1_14-MONOLITH-A3-X89_2026-11-08_» → «1_14-MONOLITH-A3-X89». Дата в
    назві повторює сьогоднішню й лише забирає місце; без голови — Sum3D-хвіст."""
    short = _DATE_TAIL.sub("", head.strip()).strip("_- ")
    return short or tail


def tile_of(card) -> Tile:
    from app.services.shop_board import board_machine

    item = board_machine(card)
    tile = Tile(key=item.key, name=item.name, state=item.state, percent=item.percent)
    tile.material = _first_material(card, item.works_are_last)
    if item.works:
        names = [w.name for w in item.works if w.name]
        line = names[0] if names else ""
        rest = re.search(r"\d+", item.extra or "")
        more = len(item.works) - 1 + (int(rest.group()) if rest else 0)
        if line and more > 0:
            line += f" +{more}"
        tile.line = line
    if item.state == "done":
        tile.lead = "зняти"
    elif item.state in _ALARM or not tile.line:
        if item.state == "fault":
            # Пояснення стану довше за плитку; суть — два слова.
            tile.line = "помилка на екрані"
        elif item.state in _ALARM:
            tile.line = item.note or item.word
        elif item.has_prog:
            tile.line = _program_short(item.prog_head, item.prog_tail)
        elif item.sum3d:
            tile.line = f"Sum3D {item.sum3d}"
        else:
            tile.line = item.word
    return tile


def board_of(cards) -> Board:
    """Плитки в порядку налаштувань — верстат завжди на своєму місці. Sisma
    тут немає: у бота для неї окремий вид."""
    return Board(tiles=[tile_of(card) for card in cards if not card.is_sisma_machine])


# ── Малювання ───────────────────────────────────────────────────────────────


@lru_cache(maxsize=32)
def _font(kind: str, size: int):
    names = {
        "bold": ("segoeuib.ttf", "seguisb.ttf", "DejaVuSans-Bold.ttf"),
        "semi": ("seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"),
        "text": ("segoeui.ttf", "DejaVuSans.ttf"),
        "mono": ("consola.ttf", "DejaVuSansMono.ttf"),
        "mono_bold": ("consolab.ttf", "consola.ttf", "DejaVuSansMono-Bold.ttf"),
    }[kind]
    for name in names:
        try:
            return ImageFont.truetype(name, size * _SCALE)
        except OSError:
            continue
    return ImageFont.load_default(size * _SCALE)


def _s(value: float) -> int:
    return int(round(value * _SCALE))


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: float) -> str:
    """Текст, що влазить у ширину; не влазить — обрізати з трикрапкою."""
    limit = width * _SCALE
    if draw.textlength(text, font=font) <= limit:
        return text
    while text and draw.textlength(text + "…", font=font) > limit:
        text = text[:-1]
    return (text.rstrip() + "…") if text else ""


def _line(draw, points, color, width):
    draw.line([(_s(x), _s(y)) for x, y in points], fill=color, width=_s(width), joint="curve")
    r = width / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse([_s(x - r), _s(y - r), _s(x + r), _s(y + r)], fill=color)


def _ring(draw, cx, cy, radius, stroke, percent, color):
    box = [_s(cx - radius), _s(cy - radius), _s(cx + radius), _s(cy + radius)]
    draw.ellipse(box, outline=_RING_TRACK, width=_s(stroke))
    share = max(0, min(100, percent)) / 100
    if share <= 0:
        return
    start, end = -90.0, -90.0 + 360.0 * share
    draw.arc(box, start=start, end=end, fill=color, width=_s(stroke))
    # Круглі кінці дуги: Pillow малює їх зрізаними.
    import math

    mid = radius - stroke / 2
    for angle in (start, end):
        a = math.radians(angle)
        x, y = cx + mid * math.cos(a), cy + mid * math.sin(a)
        r = stroke / 2
        draw.ellipse([_s(x - r), _s(y - r), _s(x + r), _s(y + r)], fill=color)


def _mark(draw, state: str, cx: float, cy: float):
    """Знак стану на місці кільця — лініями, не гліфами."""
    if state == "done":
        _line(draw, [(cx - 22, cy + 1), (cx - 6, cy + 17), (cx + 24, cy - 17)], _GREEN, 9)
    elif state == "off":
        _line(draw, [(cx - 18, cy - 18), (cx + 18, cy + 18)], _RED, 9)
        _line(draw, [(cx + 18, cy - 18), (cx - 18, cy + 18)], _RED, 9)
    elif state == "fault":
        _line(draw, [(cx, cy - 24), (cx, cy + 6)], _RED, 10)
        draw.ellipse([_s(cx - 6), _s(cy + 16), _s(cx + 6), _s(cy + 28)], fill=_RED)
    elif state in ("busy", "check"):
        draw.polygon([(_s(cx - 12), _s(cy - 16)), (_s(cx + 16), _s(cy)), (_s(cx - 12), _s(cy + 16))],
                     fill=_INK_2)
    else:
        _line(draw, [(cx - 16, cy), (cx + 16, cy)], _INK_2, 7)


def _tile(draw, tile: Tile, x: float, y: float, w: float, focus: bool):
    alarm, quiet = tile.state in _ALARM, tile.state in _QUIET
    fill, edge = _TILE, _TILE_EDGE
    if tile.state == "done":
        fill, edge = (22, 32, 26), (61, 90, 69)
    elif alarm:
        fill, edge = (32, 21, 20), (90, 47, 43)
    if focus:
        edge = _BLUSH
    draw.rounded_rectangle([_s(x), _s(y), _s(x + w), _s(y + _TILE_H)], radius=_s(_RADIUS),
                           fill=fill, outline=edge, width=_s(3 if focus else 2))

    ring_r = 60
    cx, cy = x + w - 24 - ring_r, y + _TILE_H / 2
    text_w = w - 24 - (2 * ring_r + 24 + 14)
    if tile.state == "run" and tile.percent is not None:
        color = _GREEN if tile.percent >= NEAR_DONE else _BLUSH
        _ring(draw, cx, cy, ring_r, 15, tile.percent, color)
        label = str(tile.percent)
        font = _font("bold", 44)
        tw = draw.textlength(label, font=font)
        draw.text((_s(cx) - tw / 2, _s(cy)), label, font=font, fill=_INK, anchor="lm")
    else:
        _mark(draw, tile.state, cx + ring_r - 36, cy)
        text_w = w - 24 - 96

    name_font = _font("bold", 44)
    name_ink = _INK_MUTED if quiet else _INK
    draw.text((_s(x + 24), _s(y + 18)), _fit(draw, tile.name, name_font, text_w),
              font=name_font, fill=name_ink)

    if tile.material:
        chip_font = _font("mono_bold", 29)
        chip = _fit(draw, tile.material, chip_font, text_w - 32)
        cw = draw.textlength(chip, font=chip_font) / _SCALE
        top = y + 88
        draw.rounded_rectangle([_s(x + 24), _s(top), _s(x + 24 + cw + 32), _s(top + 46)],
                               radius=_s(23), fill=_CHIP, outline=_CHIP_EDGE, width=_s(2))
        draw.text((_s(x + 40), _s(top + 23)), chip, font=chip_font, fill=_CHIP_INK, anchor="lm")

    line_font = _font("text", 34)
    base = y + _TILE_H - 26
    left = x + 24
    room = text_w
    if tile.lead:
        lead_font = _font("semi", 34)
        lead = tile.lead + (" · " if tile.line else "")
        draw.text((_s(left), _s(base)), lead, font=lead_font, fill=_GREEN, anchor="ls")
        shift = draw.textlength(lead, font=lead_font) / _SCALE
        left += shift
        room -= shift
    if tile.line:
        ink = _RED if alarm else _INK_2
        draw.text((_s(left), _s(base)), _fit(draw, tile.line, line_font, room),
                  font=line_font, fill=ink, anchor="ls")


def _summary_parts(board: Board) -> list[tuple[str, bool]]:
    parts = [(f"фрезерують {board.running} з {board.total}", False)]
    if board.done:
        parts.append((f"зняти {board.done}", False))
    if board.alarms:
        parts.append((f"тривога {board.alarms}", True))
    return parts


def summary_words(board: Board) -> str:
    """«фрезерують 3 з 8 · зняти 1 · тривога 2» — і на картинці, і в підписі."""
    return " · ".join(word for word, _ in _summary_parts(board))


def render_png(board: Board, now: datetime, focus: Optional[str] = None) -> bytes:
    rows = (len(board.tiles) + 1) // 2 or 1
    height = _HEAD + rows * (_TILE_H + _GAP) - _GAP + _PAD
    image = Image.new("RGB", (_s(WIDTH), _s(height)), _BG)
    draw = ImageDraw.Draw(image)
    step = 32
    for gx in range(0, WIDTH + 1, step):
        draw.line([(_s(gx), 0), (_s(gx), _s(height))], fill=_GRID, width=_SCALE)
    for gy in range(0, height + 1, step):
        draw.line([(0, _s(gy)), (_s(WIDTH), _s(gy))], fill=_GRID, width=_SCALE)

    draw.text((_s(_PAD), _s(26)), "Верстати", font=_font("bold", 58), fill=_INK)
    stamp = now.strftime("%d.%m · %H:%M")
    stamp_font = _font("mono", 34)
    sw = draw.textlength(stamp, font=stamp_font)
    draw.text((_s(WIDTH - _PAD) - sw, _s(46)), stamp, font=stamp_font, fill=_INK_2)
    # Червоною лише тривога, а не весь рядок: інакше «фрезерують 4» теж
    # читалось би як біда.
    left: float = _PAD
    sum_font = _font("text", 34)
    for index, (word, alarm) in enumerate(_summary_parts(board)):
        text = (" · " if index else "") + word
        draw.text((_s(left), _s(110)), text, font=sum_font, fill=_RED if alarm else _INK_2)
        left += draw.textlength(text, font=sum_font) / _SCALE

    tile_w = (WIDTH - 2 * _PAD - _GAP) / 2
    for index, tile in enumerate(board.tiles):
        col, row = index % 2, index // 2
        x = _PAD + col * (tile_w + _GAP)
        y = _HEAD + row * (_TILE_H + _GAP)
        _tile(draw, tile, x, y, tile_w, focus is not None and tile.key == focus)

    final = image.resize((WIDTH, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    final.save(out, format="PNG", optimize=True)
    return out.getvalue()


def button_label(tile: Tile, focus: bool = False) -> str:
    """Підпис кнопки верстата: коротка назва + знак стану."""
    if tile.state == "run" and tile.percent is not None:
        mark = f"{tile.percent}%"
    else:
        mark = {"done": "✓", "off": "✕", "fault": "⚠"}.get(tile.state, "·")
    return f"{'◉ ' if focus else ''}{tile.name} {mark}"
