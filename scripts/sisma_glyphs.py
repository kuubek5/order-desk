"""Навчити читач SISMA символам з реального кадру.

Тут навмисно НЕ «розпізнавання»: оператор (точніше розробник) вказує кадр,
зону і ТЕКСТ, який на ній написаний, а скрипт ріже рядок на символи й
запам'ятовує їх бітові матриці. Помилитись можна лише в тексті, і це видно
одразу — `check` перечитує кадр уже навченим читачем.

Символи зберігаються за набором (`dark` — чорний текст, `status` — кольорове
слово стану лазера) і ВИСОТОЮ: у наборі співіснують заголовки й основний
текст, і символи різної висоти не мусять порівнюватись між собою.

Використання:
    python scripts/sisma_glyphs.py learn <кадр.png> <зона> <набір> "<текст>"
    python scripts/sisma_glyphs.py show <кадр.png> <зона> <набір>
    python scripts/sisma_glyphs.py check <кадр.png>

`зона` — або назва (slice / times / laser), або чотири частки `l,t,r,b`.
Текст пишеться БЕЗ пробілів — пробіли ми не відновлюємо (див. модуль).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.machine_sisma import (  # noqa: E402
    INK_DARK,
    INK_STATUS,
    SET_WORDS,
    _line_signature,
    SISMA_GLYPHS_PATH,
    ZONE_LASER,
    ZONE_LASER_LABEL,
    ZONE_REPORT,
    ZONE_SLICE,
    ZONE_TIMES,
    _glyphs,
    _mask,
    _text_lines,
    _zone,
    load_sisma_glyphs,
    read_sisma,
)

ZONES = {"slice": ZONE_SLICE, "times": ZONE_TIMES, "laser": ZONE_LASER,
         "label": ZONE_LASER_LABEL, "report": ZONE_REPORT}
INKS = {"dark": INK_DARK, "bold": INK_DARK, "title": INK_DARK, "status": INK_STATUS}


def _resolve_zone(value: str):
    if value in ZONES:
        return ZONES[value]
    parts = [float(v) for v in value.split(",")]
    if len(parts) != 4:
        raise SystemExit("zone must be a name or four fractions l,t,r,b")
    return tuple(parts)


def _rows(image: Image.Image, zone, ink: int):
    crop = _zone(image, zone)
    mask = _mask(crop, ink)
    return mask, _text_lines(mask)


def cmd_show(args) -> int:
    image = Image.open(args.frame)
    mask, lines = _rows(image, _resolve_zone(args.zone), INKS[args.set])
    for index, (y0, y1) in enumerate(lines):
        glyphs = _glyphs(mask, y0, y1)
        widths = [len(bits[0]) for _, bits in glyphs]
        heights = sorted({len(bits) for _, bits in glyphs})
        print(f"line {index}: y={y0}-{y1} glyphs={len(glyphs)} heights={heights} widths={widths}")
        for _, bits in glyphs:
            print("  " + " ".join(str(len(bits))) + " " + "".join(
                "#" if v else "." for v in bits[0]
            ))
    return 0


def cmd_learn(args) -> int:
    image = Image.open(args.frame)
    mask, lines = _rows(image, _resolve_zone(args.zone), INKS[args.set])
    if args.line >= len(lines):
        print(f"no line {args.line}: zone has {len(lines)}")
        return 1
    y0, y1 = lines[args.line]
    glyphs = _glyphs(mask, y0, y1)
    # Токени, а не символи: подекуди дві сусідні літери намальовані злитно
    # («rt»), і такий слід чесніше вивчити як ОДИН еталон із двосимвольним
    # значенням, ніж вигадувати межу між ними. Роздільник — «|».
    tokens = args.text.split("|") if "|" in args.text else list(args.text)
    if len(glyphs) != len(tokens):
        print(f"mismatch: {len(glyphs)} glyphs, {len(tokens)} tokens")
        print("glyph widths:", [len(bits[0]) for _, bits in glyphs])
        return 1

    path = ROOT / SISMA_GLYPHS_PATH
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    bucket = data.setdefault(args.set, {})
    added = conflicts = 0
    for (_, bits), char in zip(glyphs, tokens):
        rows = ["".join("#" if v else "." for v in row) for row in bits]
        by_height = bucket.setdefault(str(len(bits)), {})
        old = by_height.get(char)
        if old is not None and old != rows:
            # Той самий символ тієї ж висоти з іншими пікселями — або текст
            # указано неправильно, або шрифт справді інший. Мовчки
            # перезаписати означало б зіпсувати вже робочі еталони.
            print(f"CONFLICT for {char!r} at height {len(bits)} — not overwritten")
            conflicts += 1
            continue
        if old is None:
            added += 1
        by_height[char] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"learned: +{added} new, {conflicts} conflicts -> {path}")
    return 1 if conflicts else 0


def cmd_learnword(args) -> int:
    """Запам'ятати СЛІД цілого слова (стан лазера). Див. SET_WORDS."""
    image = Image.open(args.frame)
    mask, lines = _rows(image, _resolve_zone(args.zone), INK_STATUS)
    if args.line >= len(lines):
        print(f"no line {args.line}: zone has {len(lines)}")
        return 1
    signature = _line_signature(mask, *lines[args.line])
    if signature is None:
        print("no ink on that line")
        return 1
    path = ROOT / SISMA_GLYPHS_PATH
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    bucket = data.setdefault(SET_WORDS, {}).setdefault(str(len(signature)), {})
    rows = ["".join("#" if v else "." for v in row) for row in signature]
    old = bucket.get(args.word)
    if old is not None and old != rows:
        print(f"CONFLICT for {args.word!r} — not overwritten")
        return 1
    bucket[args.word] = rows
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"learned word {args.word!r} ({len(signature[0])}x{len(signature)}) -> {path}")
    return 0


def cmd_check(args) -> int:
    load_sisma_glyphs.cache_clear()
    frames = [Path(args.frame)]
    if frames[0].is_dir():
        frames = sorted(frames[0].glob("*.png"))
    running = 0
    for frame in frames:
        reading = read_sisma(Image.open(frame))
        if reading.printing:
            running += 1
        print(
            f"{frame.name}: printing={reading.printing} lasing={reading.lasing} "
            f"word={reading.laser_word or '-'} "
            f"layer={reading.layer}/{reading.layers_total} pct={reading.percent} "
            f"start={reading.started_at} end={reading.ends_at}"
        )
    print(f"total {len(frames)} frames, running {running}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Teach and test the SISMA screen reader.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    show = sub.add_parser("show")
    show.add_argument("frame")
    show.add_argument("zone")
    show.add_argument("set", choices=sorted(INKS))
    show.set_defaults(func=cmd_show)

    learn = sub.add_parser("learn")
    learn.add_argument("frame")
    learn.add_argument("zone")
    learn.add_argument("set", choices=sorted(INKS))
    learn.add_argument("text")
    learn.add_argument("--line", type=int, default=0)
    learn.set_defaults(func=cmd_learn)

    word = sub.add_parser("learnword")
    word.add_argument("frame")
    word.add_argument("zone")
    word.add_argument("word")
    word.add_argument("--line", type=int, default=1)
    word.set_defaults(func=cmd_learnword)

    check = sub.add_parser("check")
    check.add_argument("frame", help="a .png or a folder of them")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
