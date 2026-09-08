"""Що читачі бачать на конкретному кадрі верстата.

Діагностика «верстат пише не те число» без здогадок: береш PNG (кадр із
`/machines` або скріншот екрана верстата) і бачиш, який саме детектор
спрацював, які пікселі він назвав заливкою, треком і фоном, і чому кандидат
відхилено.

    python -m scripts.machine_explain_frame кадр.png
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from app import machine_ocr as ocr


def _sample(px, x: int, y: int) -> tuple[int, int, int]:
    return tuple(px[x, y][:3])


def explain(path: Path) -> None:
    image = Image.open(path)
    print(f"{path.name}: {image.size[0]}x{image.size[1]}")

    old = ocr.find_progress_bar(image)
    print(f"RemiCORE (стара смуга): {old}")

    newgen = ocr.find_newgen_progress(image)
    print(f"Плаский UI (newgen):    {newgen}")

    low = ocr._find_bar_by_track(image)
    print(f"Зачіпка за трек:        {low}")

    print(f"screen_is_completed:    {ocr.screen_is_completed(image)}")
    print(f"read_progress_percent:  {ocr.read_progress_percent(image)}")

    bar = newgen or old
    if bar is None:
        return

    # Кольори по обидва боки знайденого пробігу — головна підказка, коли
    # число є, але хибне: трек, який не потрапив у діапазон `_ng_is_track`,
    # обриває смугу на краю заливки, і часткова смуга читається як 100%.
    px = image.convert("RGB").load()
    x0, top, x1, bottom = bar.box
    y = (top + bottom) // 2
    width = image.size[0]
    print(f"смуга y={y} x={x0}..{x1 - 1}")
    for label, x in (
        ("ліворуч від смуги", max(0, x0 - 4)),
        ("заливка", x0 + 2),
        ("кінець заливки+1", min(width - 1, x0 + bar.fill_width + 1)),
        ("праворуч від смуги", min(width - 1, x1 + 3)),
    ):
        p = _sample(px, x, y)
        marks = []
        if ocr._ng_is_fill(p):
            marks.append("fill")
        if ocr._ng_is_track(p):
            marks.append("track")
        if ocr._ng_is_bg(p):
            marks.append("bg")
        print(f"  {label:<20} x={x:<5} {p} {'/'.join(marks) or 'нічого'}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for name in argv:
        explain(Path(name))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
