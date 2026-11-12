"""Набір для малювання карток бота в стилі CRM (власник 12.11.26).

Усі картинки бота — одна візуальна мова: теплий темний фон, панелі й рядки як
у списку «Нові з пошти», ті самі кольори матеріалів і ті самі шрифти, що в
CRM. Кольори зняті з екрана CRM (тема «forge»), шрифти — Golos Text і
JetBrains Mono, склеєні з підмножин CRM в один файл на накреслення
(`app/static/fonts/bot/`, їдуть з інсталятором разом із `app/static`).

Малюємо вдвічі більше й зменшуємо (згладжування). Модуль лише малює: даних
не збирає, у базу й на диск не пише.
"""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Iterable, Optional, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from app.runtime import resource_path

WIDTH = 1080
SCALE = 2
PAD = 44

Color = tuple[int, int, int]

BG: Color = (11, 8, 5)
PANEL: Color = (15, 11, 6)
PANEL_HI: Color = (27, 17, 9)
TILE: Color = (20, 15, 9)
LINE: Color = (44, 34, 20)
ROW_LINE: Color = (36, 27, 16)
TAB_ON: Color = (63, 49, 27)
INK: Color = (251, 243, 232)
INK_2: Color = (165, 155, 136)
INK_3: Color = (125, 113, 95)
ACCENT: Color = (255, 180, 84)
ORANGE: Color = (255, 140, 60)
GREEN: Color = (99, 200, 139)
RED: Color = (240, 138, 120)
ZR: Color = (143, 191, 230)
PM: Color = (216, 180, 106)
TI: Color = (99, 200, 139)
WAX: Color = (89, 214, 214)

#: Ключ групи матеріалу → (підпис маркера, колір, фон маркера).
MATERIALS: dict[str, tuple[str, Color, Color]] = {
    "zr": ("Zr", ZR, (32, 35, 34)),
    "pm": ("PMMA", PM, (41, 33, 19)),
    "ti": ("Ti", TI, (22, 38, 29)),
    "wx": ("Wax", WAX, (18, 36, 38)),
}

_FONT_FILES = {
    "x": ("KMillSans-ExtraBold.ttf", ("segoeuib.ttf", "DejaVuSans-Bold.ttf")),
    "s": ("KMillSans-SemiBold.ttf", ("seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf")),
    "t": ("KMillSans-Regular.ttf", ("segoeui.ttf", "DejaVuSans.ttf")),
    "m": ("KMillMono-Medium.ttf", ("consola.ttf", "DejaVuSansMono.ttf")),
}


def s(value: float) -> int:
    return int(round(value * SCALE))


def mix(color: Color, base: Color, share: float) -> Color:
    """`share` кольору поверх `base` — замість прозорості, якої в RGB немає."""
    return tuple(int(round(c * share + b * (1 - share))) for c, b in zip(color, base))  # type: ignore[return-value]


@lru_cache(maxsize=256)
def font(kind: str, size: int):
    """Шрифт CRM; немає файлу — системний, щоб картинка все одно вийшла."""
    own, fallbacks = _FONT_FILES[kind]
    candidates = [str(resource_path(f"app/static/fonts/bot/{own}")), *fallbacks]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size * SCALE)
        except OSError:
            continue
    return ImageFont.load_default(size * SCALE)


@lru_cache(maxsize=16)
def _photo(name: str) -> Optional[Image.Image]:
    try:
        return Image.open(resource_path(f"app/static/img/{name}")).convert("RGB")
    except OSError:
        return None


Span = tuple[str, str, int, Color]


class Canvas:
    """Полотно картки: координати — у логічних пікселях (ширина 1080)."""

    def __init__(self, height: float, bg: Color = BG):
        self.height = int(round(height))
        self.im = Image.new("RGB", (s(WIDTH), s(self.height)), bg)
        self.d = ImageDraw.Draw(self.im)

    # ── примітиви ──────────────────────────────────────────────────────────
    def glow(self, color: Color = ACCENT, strength: float = 0.11) -> None:
        """Тепле світіння зліва вгорі, як на екранах CRM."""
        w, h = s(1700), s(1000)
        mask = ImageOps.invert(Image.radial_gradient("L")).resize((w, h))
        # Градієнт Pillow на середині краю ще не чорний — зрізаємо хвіст, інакше
        # видно прямокутну межу світіння.
        mask = mask.point(lambda v: int(max(0, v - 84) * 255 / 171 * strength))
        self.im.paste(Image.new("RGB", (w, h), color), (s(-750), s(-560)), mask)

    def box(self, x: float, y: float, w: float, h: float, *, fill: Optional[Color] = None,
            outline: Optional[Color] = None, radius: float = 0, width: float = 1.5) -> None:
        self.d.rounded_rectangle([s(x), s(y), s(x + w), s(y + h)], radius=s(radius), fill=fill,
                                 outline=outline, width=max(1, s(width)) if outline else 0)

    def line(self, x1: float, y1: float, x2: float, y2: float, fill: Color, width: float = 1.5) -> None:
        self.d.line([(s(x1), s(y1)), (s(x2), s(y2))], fill=fill, width=max(1, s(width)))

    def dot(self, cx: float, cy: float, r: float, fill: Color) -> None:
        self.d.ellipse([s(cx - r), s(cy - r), s(cx + r), s(cy + r)], fill=fill)

    def width(self, text: str, kind: str, size: int) -> float:
        return self.d.textlength(text, font=font(kind, size)) / SCALE

    def fit(self, text: str, kind: str, size: int, room: float) -> str:
        """Текст, що влазить у `room`: зайве — трикрапкою."""
        if room <= 0:
            return ""
        if self.width(text, kind, size) <= room:
            return text
        while text and self.width(text + "…", kind, size) > room:
            text = text[:-1]
        return (text.rstrip() + "…") if text else ""

    def text(self, x: float, y: float, text: str, kind: str = "t", size: int = 30,
             fill: Color = INK, anchor: str = "ls") -> float:
        """Текст на базовій лінії `y` (anchor як у Pillow). Повертає ширину."""
        self.d.text((s(x), s(y)), text, font=font(kind, size), fill=fill, anchor=anchor)
        return self.width(text, kind, size)

    def spans(self, x: float, y: float, parts: Iterable[Span]) -> float:
        """Рядок із шматків різного кольору й накреслення; повертає кінець."""
        for text, kind, size, fill in parts:
            x += self.text(x, y, text, kind, size, fill)
        return x

    def photo(self, name: str, x: float, y: float, w: float, h: float, *, radius: float = 0,
              opacity: float = 0.6, fade_from: float = 0.25) -> None:
        """Фото з `app/static/img` у прямокутнику: зліва зникає, справа видно."""
        source = _photo(name)
        if source is None:
            return
        size = (s(w), s(h))
        picture = ImageOps.fit(source, size, Image.Resampling.LANCZOS)
        fade = Image.linear_gradient("L").rotate(90, expand=True).resize(size)  # 0 зліва → 255 справа
        cut = int(255 * fade_from)
        fade = fade.point(lambda v: 0 if v <= cut else int((v - cut) * 255 / (255 - cut) * opacity))
        shape = Image.new("L", size, 0)
        ImageDraw.Draw(shape).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=s(radius), fill=255)
        self.im.paste(picture, (s(x), s(y)), ImageChops.multiply(fade, shape))

    def ring(self, cx: float, cy: float, r: float, stroke: float, share: float, color: Color,
             track: Color = LINE, label: str = "", label_size: int = 0) -> None:
        box = [s(cx - r), s(cy - r), s(cx + r), s(cy + r)]
        self.d.arc(box, 0, 360, fill=track, width=s(stroke))
        share = max(0.0, min(1.0, share))
        if share > 0:
            end = -90 + 360 * share
            self.d.arc(box, -90, end, fill=color, width=s(stroke))
            # Круглі кінці дуги.
            import math

            mid = r - stroke / 2
            for angle in (-90, end):
                rad = math.radians(angle)
                self.dot(cx + mid * math.cos(rad), cy + mid * math.sin(rad), stroke / 2, color)
        if label:
            self.text(cx, cy, label, "x", label_size or int(r * 0.6), INK, anchor="mm")

    def png(self) -> bytes:
        final = self.im.resize((WIDTH, self.height), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        final.save(out, format="PNG", optimize=True)
        return out.getvalue()

    # ── складені елементи ──────────────────────────────────────────────────
    def check(self, cx: float, cy: float, size: float, color: Color, width: float = 3.2) -> None:
        k = size / 2
        self.d.line([(s(cx - k), s(cy)), (s(cx - k * 0.25), s(cy + k * 0.7)), (s(cx + k), s(cy - k * 0.7))],
                    fill=color, width=s(width), joint="curve")

    def badge(self, x: float, cy: float, ok: bool, size: float = 38) -> None:
        """Значок готовності, як у списку пошти CRM: ✓ зелений або ! помаранчевий."""
        top = cy - size / 2
        if ok:
            self.box(x, top, size, size, fill=(18, 38, 26), outline=(47, 107, 70), radius=9)
            self.check(x + size / 2, cy + 1, size * 0.5, GREEN)
        else:
            self.box(x, top, size, size, fill=(46, 28, 13), outline=(107, 61, 24), radius=9)
            self.text(x + size / 2, cy, "!", "x", int(size * 0.66), ORANGE, anchor="mm")

    def marker(self, x: float, cy: float, key: str, code: str = "", *, tag_size: int = 23,
               code_size: int = 26) -> float:
        """Маркер матеріалу (`Zr A3`): кольорова риска зліва, як у CRM. Ширина."""
        if key not in MATERIALS:
            return 0.0
        tag, color, back = MATERIALS[key]
        w = 12 + self.width(tag, "m", tag_size) + (9 + self.width(code, "m", code_size) if code else 0) + 11
        h = 38
        self.box(x, cy - h / 2, w, h, fill=back, radius=8)
        self.box(x, cy - h / 2, 4, h, fill=color, radius=2)
        cursor = x + 12 + self.text(x + 12, cy + 9, tag, "m", tag_size, color)
        if code:
            self.text(cursor + 9, cy + 9, code, "m", code_size, INK)
        return w

    def disc(self, x: float, y: float, w: float, color: Color, base: Color = PANEL) -> None:
        """Ізометричний диск — значок матеріалу зі смуги фільтра пошти."""
        ry, side = w * 0.16, w * 0.33
        top = y + ry
        body = mix(color, base, 0.22)
        self.d.ellipse([s(x), s(top + side - ry), s(x + w), s(top + side + ry)], fill=body, outline=color, width=s(1.7))
        self.d.rectangle([s(x), s(top), s(x + w), s(top + side)], fill=body)
        self.line(x, top, x, top + side, color, 1.7)
        self.line(x + w, top, x + w, top + side, color, 1.7)
        self.d.ellipse([s(x), s(top - ry), s(x + w), s(top + ry)], fill=mix(color, base, 0.10), outline=color, width=s(1.7))

    def pill(self, x_right: float, cy: float, text: str, *, kind: str = "m", size: int = 28,
             fill: Color = INK_2) -> None:
        w = self.width(text, kind, size) + 40
        self.box(x_right - w, cy - 27, w, 54, fill=mix(INK, BG, 0.03), outline=LINE, radius=27)
        self.text(x_right - w / 2, cy + 1, text, kind, size, fill, anchor="mm")

    def tile(self, x: float, y: float, w: float, h: float, *, tone: str = "") -> None:
        fill, edge = TILE, LINE
        if tone == "alarm":
            fill, edge = (34, 19, 14), (112, 58, 42)
        elif tone == "ok":
            fill, edge = (16, 30, 21), (47, 107, 70)
        self.box(x, y, w, h, fill=fill, outline=edge, radius=26)


def header(c: Canvas, title: str, subtitle: Sequence[Span] | str, stamp: str) -> float:
    """Шапка картки: назва, рядок-підсумок, час. Повертає y початку вмісту."""
    c.glow()
    c.text(PAD, 98, title, "x", 62)
    c.pill(WIDTH - PAD, 74, stamp)
    if isinstance(subtitle, str):
        subtitle = [(subtitle, "t", 30, INK_2)]
    c.spans(PAD, 150, subtitle)
    return 182.0


def sub(text: str, fill: Color = INK_2, kind: str = "t") -> Span:
    """Шматок рядка-підсумку типового розміру."""
    return (text, kind, 30, fill)
