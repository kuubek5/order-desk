# -*- coding: utf-8 -*-
"""Знак KuubMill — монограма KM. Одна геометрія, з неї весь комплект.

Форму обрано власником з десяти напрямів. Вона намальована вектором, тому
джерело тут — координати, а не картинка: SVG, .ico, PNG і банери інсталятора
рендеряться з одного опису, і правка форми розходиться скрізь одночасно.

ОПТИЧНЕ ВАЖЧАННЯ: на 16 px штрих у пропорції великого розміру зникає, тому
товщина зростає для малих іконок (див. stroke_ratio). Це стандартна практика
іконок, а не милиця.
"""
import pathlib
import struct
import io
from PIL import Image, ImageDraw

ROOT = pathlib.Path(r"P:\AI-Projects\CRM_Laba")

# ── геометрія в сітці 32×32 ────────────────────────────────────────────────
# K: стійка + два промені, що сходяться в середині. M: дві стійки і клин.
STROKES = [
    [(5, 6), (5, 26)],                                   # стійка K
    [(14, 6), (5, 16)],                                  # верхній промінь K
    [(5, 16), (14, 26)],                                 # нижній промінь K
    [(18, 26), (18, 6), (23, 15.5), (28, 6), (28, 26)],  # M
]
BOX = (5, 6, 28, 26)          # реальні межі малюнка
GRID = 32

ACCENT = (45, 212, 191)       # --accent-b, бірюзовий канон
TILE_BG = (13, 17, 23)        # --bg


def stroke_ratio(size: int) -> float:
    """Товщина штриха відносно розміру іконки."""
    if size >= 96:
        return 0.062
    if size >= 48:
        return 0.075
    if size >= 32:
        return 0.095
    return 0.125          # 16–24 px: без потовщення знак розсипається


def render(size: int, fg=ACCENT, bg=None, fill_ratio=0.74, tile=False):
    """Намалювати знак у квадраті size×size."""
    ss = 8 if size <= 64 else 4          # суперсемплінг заради чистих країв
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if tile:
        r = int(S * 0.22)
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=bg + (255,))

    x0, y0, x1, y1 = BOX
    gw, gh = x1 - x0, y1 - y0
    scale = S * fill_ratio / max(gw, gh)
    ox = (S - gw * scale) / 2 - x0 * scale
    oy = (S - gh * scale) / 2 - y0 * scale

    w = max(1.0, stroke_ratio(size) * S)
    rr = w / 2
    col = fg + (255,)

    for pts in STROKES:
        p = [(x * scale + ox, y * scale + oy) for x, y in pts]
        d.line(p, fill=col, width=int(round(w)), joint="curve")
        # Pillow не вміє круглих кінців — домальовуємо кружечки на вузлах,
        # інакше на згинах M і K лишаються зрізи.
        for cx, cy in p:
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=col)

    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: pathlib.Path, sizes, **kw):
    """Зібрати .ico вручну.

    Pillow при save(sizes=…) масштабує ОДНЕ зображення, тобто 16 px вийшов би
    зменшеною копією великого й замилився. Тут кожен розмір мальований своєю
    товщиною штриха, тому контейнер складається руками: заголовок, таблиця
    записів, далі PNG-и (їх формат .ico приймає, починаючи з Vista).
    """
    blobs = []
    for s in sizes:
        buf = io.BytesIO()
        render(s, **kw).save(buf, "PNG", optimize=True)
        blobs.append(buf.getvalue())

    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    offset = 6 + 16 * len(sizes)
    for s, b in zip(sizes, blobs):
        dim = 0 if s >= 256 else s
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(b), offset)
        offset += len(b)
    for b in blobs:
        out += b
    path.write_bytes(bytes(out))
    return len(out)


def write_svg(path: pathlib.Path):
    """Майстер-вектор у currentColor — рейка й фавікон беруть його."""
    parts = []
    for pts in STROKES:
        d = "M" + " L".join(f"{x} {y}" for x, y in pts)
        parts.append(f'  <path d="{d}"/>')
    body = "\n".join(parts)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}" '
        f'fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">\n{body}\n</svg>\n'
    )
    path.write_text(svg, encoding="utf-8")
    return len(svg)


def write_wizard_bmp(path: pathlib.Path, w: int, h: int, mark_frac: float):
    """Банер майстра встановлення. Inno читає лише BMP, тому без альфи."""
    img = Image.new("RGB", (w, h), TILE_BG)
    d = ImageDraw.Draw(img)
    step = max(12, w // 10)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(20, 27, 36))
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(20, 27, 36))

    m = int(min(w, h) * mark_frac)
    mark = render(m, fg=ACCENT)
    img.paste(mark, ((w - m) // 2, (h - m) // 2), mark)
    img.save(path, "BMP")
    return path.stat().st_size


if __name__ == "__main__":
    # 1 · майстер-вектор (рейка, фавікон, сторінка входу)
    p = ROOT / "app" / "static" / "img" / "logo-kmill.svg"
    print("svg   ", p.name, write_svg(p), "байт")

    # 2 · іконка застосунку: exe, трей, ярлик, інсталятор.
    #     Ім'я файлу лишається старим — це технічний ідентифікатор,
    #     на нього дивляться .spec, .iss і windows_launcher.
    p = ROOT / "assets" / "orderdesk.ico"
    n = write_ico(p, [16, 24, 32, 48, 64, 128, 256], tile=True, bg=TILE_BG, fill_ratio=0.62)
    print("ico   ", p.name, n, "байт")

    # 3 · фавікон вкладки
    p = ROOT / "app" / "static" / "favicon.ico"
    n = write_ico(p, [16, 32, 48], tile=True, bg=TILE_BG, fill_ratio=0.62)
    print("favico", p.name, n, "байт")

    # 4 · знак у рейці (запасний растр; сама рейка малює інлайн-SVG)
    p = ROOT / "app" / "static" / "img" / "app-icon.png"
    render(256, fill_ratio=0.8).save(p, optimize=True)
    print("png   ", p.name, p.stat().st_size, "байт")

    # 5 · банери майстра встановлення (розміри задані Inno Setup)
    for name, w, h, frac in (("wizard-large.bmp", 164, 314, 0.72),
                             ("wizard-small.bmp", 55, 55, 0.78)):
        p = ROOT / "installer" / name
        print("bmp   ", name, write_wizard_bmp(p, w, h, frac), "байт")

    # 6 · контактний аркуш для перевірки оком
    sizes = [16, 24, 32, 48, 64, 128]
    sheet = Image.new("RGB", (sum(sizes) + 40 * len(sizes), 320), (11, 15, 20))
    x = 20
    for s in sizes:
        sheet.paste(render(s, tile=True, bg=TILE_BG, fill_ratio=0.62), (x, 40), render(s, tile=True, bg=TILE_BG, fill_ratio=0.62))
        m = render(s)
        sheet.paste(m, (x, 200), m)
        x += s + 40
    sheet.save(ROOT / "design" / "_brandmark_sheet.png")
    print("аркуш  design/_brandmark_sheet.png")
