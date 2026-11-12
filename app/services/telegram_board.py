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

import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Optional

from PIL import ImageDraw

from app.services import telegram_draw as td

# Ширина готової картинки. На телефоні фото займає ~330 pt, тобто кожен
# піксель тут ≈ 0.3 pt: шрифт 34 px читається як 11 pt. Розміри нижче
# підібрані під це, а не під повнорозмірний перегляд.
WIDTH = 1080
_SCALE = 2  # малюємо вдвічі більше й зменшуємо — згладжування

# Розміри й кольори — спільні з рештою карток бота (`telegram_draw`, стиль
# CRM, власник 12.11.26): теплий темний фон, ті самі шрифти.
_PAD = td.PAD
_GAP = 16
_HEAD = 182
_TILE_H = 214
_RADIUS = 26

_BG = td.BG
_GRID = (16, 12, 7)  # сітка старої картинки пошти (режим без карток)
_TILE = td.TILE
_TILE_EDGE = td.LINE
_INK = td.INK
_INK_2 = td.INK_2
_INK_MUTED = td.INK_2
_BLUSH = td.ACCENT
_GREEN = td.GREEN
_RED = td.RED
_AMBER = td.ACCENT
_CHIP = (41, 33, 19)
_CHIP_EDGE = (74, 58, 32)
_CHIP_INK = td.INK
_RING_TRACK = td.LINE

# Відсоток, від якого кільце зеленіє: «скоро знімати».
NEAR_DONE = 85

# Тон плитки за `state_key`. Невідомий новий ключ читається як спокійний,
# а не як «фрезерує» — те саме правило, що на телевізорі.
_ALARM = {"off", "fault"}
_QUIET = {"idle", "busy", "check", "wait", "unreadable"}

_DATE_TAIL = re.compile(r"[_-]?\d{4}-\d{2}-\d{2}[_-]?$")
# Початок назви програми «1_14-…» = одиниць на диску _ номер диска (власник
# 08.11.26: «одна одиниця фрезерується в 14-му диску»).
_DISC_MARK = re.compile(r"^\s*(\d{1,3})_(\d{1,4})-")


def disc_mark(program: Optional[str]) -> Optional[tuple[int, int]]:
    """(одиниць, номер диска) з початку назви програми; немає позначки — None."""
    match = _DISC_MARK.match(program or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


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
    mark = disc_mark(item.prog_head)
    if mark is not None and tile.material:
        # Номер диска поруч із матеріалом: за ним диск шукають на полиці.
        tile.material = f"{tile.material} · №{mark[1]}"
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
        elif mark is not None:
            tile.line = f"диск №{mark[1]} · {mark[0]} од."
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
    """Шрифти CRM з `telegram_draw` (Golos Text, JetBrains Mono)."""
    return td.font({"bold": "x", "semi": "s", "text": "t", "mono": "m", "mono_bold": "m"}[kind], size)


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
        fill, edge = (16, 30, 21), (47, 107, 70)
    elif alarm:
        fill, edge = (34, 19, 14), (112, 58, 42)
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
    canvas = td.Canvas(height)
    draw = canvas.d
    # Червоною лише тривога, а не весь рядок: інакше «фрезерують 4» теж
    # читалось би як біда.
    parts = [((" · " if index else "") + word, "t", 30, _RED if alarm else _INK_2)
             for index, (word, alarm) in enumerate(_summary_parts(board))]
    td.header(canvas, "Верстати", parts, now.strftime("%d.%m · %H:%M"))

    tile_w = (WIDTH - 2 * _PAD - _GAP) / 2
    for index, tile in enumerate(board.tiles):
        col, row = index % 2, index // 2
        x = _PAD + col * (tile_w + _GAP)
        y = _HEAD + row * (_TILE_H + _GAP)
        _tile(draw, tile, x, y, tile_w, focus is not None and tile.key == focus)

    return canvas.png()


def button_label(tile: Tile, focus: bool = False) -> str:
    """Підпис кнопки верстата: коротка назва + знак стану."""
    if tile.state == "run" and tile.percent is not None:
        mark = f"{tile.percent}%"
    else:
        mark = {"done": "✓", "off": "✕", "fault": "⚠"}.get(tile.state, "·")
    return f"{'◉ ' if focus else ''}{tile.name} {mark}"
