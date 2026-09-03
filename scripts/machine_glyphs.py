"""Навчити читання ПІДПИСУ смуги верстата: подивитись, перевірити, донавчити.

Навіщо. Відсоток зараз береться з ГЕОМЕТРІЇ смуги (частка заливки), і це вже
сім разів ламалось об чергове синє щось на екрані. Підпис усередині смуги
(«43%») — те, що верстат сам про себе пише, тобто надійніше за будь-яку нашу
оцінку. Читається він шаблонами цифр, знятими з РЕАЛЬНИХ кадрів: жоден
системний шрифт не збігається (перевірено на печах — найкращий кандидат,
Arial Bold 23, розходився на 20% пікселів).

Три режими над PNG-кадром верстата (такий кадр пише сам застосунок у
machine_frames, або його збирає scripts/machine_collect_frames.py):

    python scripts/machine_glyphs.py show   кадр.png          → вирізати підпис у файл
    python scripts/machine_glyphs.py check  кадр.png          → що ми з нього читаємо
    python scripts/machine_glyphs.py learn  кадр.png 43       → додати еталони цифр

Порядок роботи на робочому ПК:

 1. `python scripts/machine_collect_frames.py <адреса> <ТОКЕН> --minutes 40`
    — збирає кадри з різними відсотками за одну програму;
 2. на кожен зібраний кадр — `learn` із тим числом, яке на ньому видно;
 3. `check` на кількох кадрах: підпис і геометрія мають сходитись.

`learn` НЕ перезаписує наявний еталон — додає варіант лише якщо бітмапа нова,
тому запускати його двічі на тому самому кадрі безпечно.

Складність рівно одна, і її варто знати: підпис стоїть по центру смуги, тому
на частковому прогресі він РОЗРІЗАНИЙ межею заливки — ліва половина біла на
синьому, права темна на світлому. Скрипт це вже враховує (маска будується до
розрізання на символи), але саме тому потрібні кадри з РІЗНИМИ відсотками, а
не один: цифра на межі виглядає інакше, ніж та сама цифра цілком на заливці.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.machine_ocr import (  # noqa: E402
    MACHINE_GLYPHS_PATH,
    _bitmap,
    _caption_ink,
    _segments,
    caption_mask,
    find_progress_bar,
    load_machine_glyphs,
    read_caption_percent,
)

GLYPHS_FILE = Path(__file__).resolve().parent.parent / MACHINE_GLYPHS_PATH


def _bar_and_mask(path: Path):
    image = Image.open(path).convert("RGB")
    bar = find_progress_bar(image)
    if bar is None:
        print(
            f"{path.name}: смугу прогресу не знайдено — кадр не годиться "
            f"(верстат стоїть або програма не запущена?)",
            file=sys.stderr,
        )
        return None, None, None
    mask = caption_mask(image, bar)
    if mask is None:
        print(f"{path.name}: смуга завузька для підпису", file=sys.stderr)
        return image, bar, None
    return image, bar, mask


def cmd_show(args) -> int:
    path = Path(args.frame)
    image, bar, mask = _bar_and_mask(path)
    if mask is None:
        return 2
    out = Path(args.out or f"{path.stem}-caption.png")
    # Збільшуємо: підпис 8-10px заввишки, і оком на ньому нічого не видно.
    mask.resize((mask.width * 6, mask.height * 6), Image.NEAREST).save(out)
    boxes = _segments(mask, _caption_ink)
    print(f"смуга: {bar.percent}% (геометрія), контейнер {bar.container_width}px")
    print(f"символів у підписі: {len(boxes)}")
    for left, top, right, bottom in boxes:
        print(f"  ширина {right - left}, висота {bottom - top}")
    print(f"збережено: {out}")
    return 0


def cmd_check(args) -> int:
    rc = 0
    for name in args.frames:
        path = Path(name)
        image, bar, mask = _bar_and_mask(path)
        if bar is None:
            rc = 2
            continue
        caption = read_caption_percent(image, bar)
        geometry = bar.percent
        mark = "="
        if caption is None:
            mark = "?"
        elif caption != geometry:
            mark = "≠"
        print(
            f"{path.name}: підпис "
            f"{caption if caption is not None else '—'}%  {mark}  "
            f"геометрія {geometry}%"
        )
        if caption is None:
            # Не помилка: до навчання це очікуваний стан. Але видно, скільки
            # символів лишились невпізнаними — саме їх і треба донавчити.
            if mask is not None:
                print(f"    (символів у підписі: {len(_segments(mask, _caption_ink))})")
    return rc


def cmd_learn(args) -> int:
    path = Path(args.frame)
    image, bar, mask = _bar_and_mask(path)
    if mask is None:
        return 2

    digits = str(args.text).strip().replace(" ", "").rstrip("%")
    if not digits.isdigit():
        print(f"Очікується число, а не «{args.text}»", file=sys.stderr)
        return 2

    boxes = _segments(mask, _caption_ink)
    # Знак «%» РОЗПАДАЄТЬСЯ на кілька сегментів при різанні по колонках: два
    # кружечки й скісна риска не мають спільної колонки. Тому цифри беремо за
    # позицією зліва, а ВСІ хвостові сегменти вчимо як варіанти одного символу
    # «%» — при читанні вони однаково пропускаються. Вимога рівно трьох
    # сегментів на «43%» провалювалась би на кожному кадрі.
    if len(boxes) < len(digits) + 1:
        print(
            f"Не збігається: у підписі {len(boxes)} сегментів, а «{digits}%» "
            f"потребує щонайменше {len(digits) + 1}.\n"
            f"Розміри знайдених: {[(b[2] - b[0], b[3] - b[1]) for b in boxes]}\n"
            f"Порада: подивіться `show` на цей кадр — можливо, число інше.",
            file=sys.stderr,
        )
        return 2
    expected = digits + "%" * (len(boxes) - len(digits))

    store = json.loads(GLYPHS_FILE.read_text(encoding="utf-8")) if GLYPHS_FILE.exists() else {}
    fonts = store.setdefault("fonts", {})
    added = 0
    for box, char in zip(boxes, expected):
        bitmap = _bitmap(mask, box, _caption_ink)
        height = str(len(bitmap))
        variants = fonts.setdefault(height, {}).setdefault(char, [])
        if bitmap in variants:
            continue
        variants.append(bitmap)
        added += 1
        print(f"  + «{char}» висота {height}, ширина {len(bitmap[0])}")

    if added:
        GLYPHS_FILE.parent.mkdir(parents=True, exist_ok=True)
        GLYPHS_FILE.write_text(
            json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        load_machine_glyphs.cache_clear()
    print(f"Додано нових еталонів: {added}")
    for height, chars in sorted(fonts.items(), key=lambda item: int(item[0])):
        print(f"  висота {height}: {''.join(sorted(chars))}")
    missing = _missing_digits(fonts)
    if missing:
        print(f"Ще бракує цифр: {', '.join(missing)} — потрібні кадри з ними.")
    else:
        print("Усі цифри 0-9 є.")
    return 0


def _missing_digits(fonts: dict) -> list[str]:
    seen = set()
    for chars in fonts.values():
        seen.update(chars)
    return [d for d in "0123456789" if d not in seen]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    show = sub.add_parser("show", help="Вирізати підпис у файл (збільшено)")
    show.add_argument("frame")
    show.add_argument("--out", default=None)
    show.set_defaults(func=cmd_show)

    check = sub.add_parser("check", help="Показати, що ми читаємо з кадрів")
    check.add_argument("frames", nargs="+")
    check.set_defaults(func=cmd_check)

    learn = sub.add_parser("learn", help="Додати еталони цифр із кадру")
    learn.add_argument("frame")
    learn.add_argument("text", help="Що написано в підписі, напр. 43 або 43%%")
    learn.set_defaults(func=cmd_learn)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
