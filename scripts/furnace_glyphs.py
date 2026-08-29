"""Калібрування читання табло печі: подивитись, перевірити, донавчити.

Три режими, усі — над одним PNG-кадром панелі 800×600 (такий кадр пише сам
застосунок у теку frames, або його можна вирізати зі скріншота RealVNC
ключем --crop):

    python scripts/furnace_glyphs.py zones  кадр.png            → кадр із намальованими зонами
    python scripts/furnace_glyphs.py check  кадр.png            → що ми з нього читаємо
    python scripts/furnace_glyphs.py learn  кадр.png temp 1183  → додати еталони цифр

Навіщо `learn`. Еталони цифр знімаються з реального табло, а не малюються:
шрифт панелі не збігається з жодним системним (перевірено — найкращий
кандидат, Arial Bold 23, розходиться на 20% пікселів). Дрібний шрифт
(годинники, «срок») укомплектований повністю з двох калібрувальних кадрів.
Великому червоному шрифту температури бракує 1,2,3,6,8 — вони просто не
трапились на тих двох кадрах. Один прогін `learn` на будь-якому кадрі, де ці
цифри видно, закриває питання назавжди.

`learn` НЕ перезаписує наявний еталон: він додає варіант лише якщо бітмапа
нова. Тому запустити його двічі на тому самому кадрі — безпечно.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.furnace_ocr import (  # noqa: E402
    GLYPHS_PATH,
    INKS,
    PANEL_SIZE,
    ZONES,
    _bitmap,
    _segments,
    format_remaining,
    load_glyphs,
    read_panel,
)

GLYPHS_FILE = Path(__file__).resolve().parent.parent / GLYPHS_PATH


def _load_panel(path: Path, crop: str | None) -> Image.Image:
    image = Image.open(path).convert("RGB")
    if crop:
        left, top = (int(part) for part in crop.split(","))
        image = image.crop((left, top, left + PANEL_SIZE[0], top + PANEL_SIZE[1]))
    return image


def cmd_zones(args) -> int:
    panel = _load_panel(Path(args.frame), args.crop)
    canvas = panel.copy()
    draw = ImageDraw.Draw(canvas)
    for name, zone in ZONES.items():
        draw.rectangle(zone.rect, outline=(255, 0, 255))
        draw.text((zone.rect[0] + 1, max(0, zone.rect[1] - 11)), name, fill=(255, 0, 255))
    out = Path(args.out or Path(args.frame).with_suffix(".zones.png"))
    canvas.save(out)
    print(f"Зони намальовано: {out}")
    return 0


def cmd_check(args) -> int:
    panel = _load_panel(Path(args.frame), args.crop)
    reading = read_panel(panel)
    print(f"Статус:      {reading.status}   сигнали: {reading.signals}")
    print(f"Температура: {reading.temp_c}   (сире: {reading.fields.get('temp')})")
    print(
        f"Лишилось:    {format_remaining(reading.remaining_seconds)}"
        f"   (сире: {reading.fields.get('remaining')})"
    )
    print(f"Команда:     {reading.command}")
    print(f"Пройшло:     {format_remaining(reading.elapsed_seconds)}")
    for warning in reading.warnings:
        print(f"  ! {warning}")
    return 0


def cmd_learn(args) -> int:
    panel = _load_panel(Path(args.frame), args.crop)
    zone = ZONES[args.zone]
    ink = INKS[zone.ink]
    crop = panel.crop(zone.rect)
    expected = args.text.replace(" ", "")
    boxes = _segments(crop, ink)
    if len(boxes) != len(expected):
        print(
            f"Не збігається: у зоні {len(boxes)} символів, а в «{expected}» — {len(expected)}.\n"
            f"Розміри знайдених символів: {[(b[2] - b[0], b[3] - b[1]) for b in boxes]}",
            file=sys.stderr,
        )
        return 2

    store = json.loads(GLYPHS_FILE.read_text(encoding="utf-8")) if GLYPHS_FILE.exists() else {}
    fonts = store.setdefault("fonts", {})
    added = 0
    for box, char in zip(boxes, expected):
        # Літери теж: «C0» проти «T008.A990» — це третій сигнал «йде/не йде»,
        # і без літер поточна команда ніколи не прочиталась би.
        if not char.isalnum():
            continue
        bitmap = _bitmap(crop, box, ink)
        height = str(len(bitmap))
        variants = fonts.setdefault(height, {}).setdefault(char, [])
        if bitmap in variants:
            continue
        variants.append(bitmap)
        added += 1
        print(f"  + «{char}» висота {height}, ширина {len(bitmap[0])}")

    if added:
        GLYPHS_FILE.write_text(
            json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        load_glyphs.cache_clear()
    print(f"Додано нових еталонів: {added}")
    for height, chars in sorted(fonts.items(), key=lambda item: int(item[0])):
        print(f"  висота {height}: {''.join(sorted(chars))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--crop",
        help="Зсув панелі всередині скріншота, «x,y» — коли кадр вирізається з вікна RealVNC",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    zones = sub.add_parser("zones", help="Намалювати зони поверх кадру")
    zones.add_argument("frame")
    zones.add_argument("--out")
    zones.set_defaults(func=cmd_zones)

    check = sub.add_parser("check", help="Показати, що ми читаємо з кадру")
    check.add_argument("frame")
    check.set_defaults(func=cmd_check)

    learn = sub.add_parser("learn", help="Додати еталони цифр із кадру")
    learn.add_argument("frame")
    learn.add_argument("zone", choices=sorted(ZONES))
    learn.add_argument("text", help="Що насправді написано в зоні, напр. 1183 або 00:26:59")
    learn.set_defaults(func=cmd_learn)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
