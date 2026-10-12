"""Навчити читання назви програми з екрана JOBS верстатів нового покоління.

Два режими над PNG-кадром верстата (такий кадр пише сам застосунок у
machine_frames або в теку калібрування) чи над вирізом рядка назви:

    python scripts/newgen_glyphs.py check  кадр.png
        → що ми з нього читаємо (дата, Sum3D ID і повна назва або «не прочитано»)
    python scripts/newgen_glyphs.py learn  кадр.png "1_18-EMOTIONS-A1-X193_2026-09-04_12-57-22.ISO"
        → додати еталони цифр і літер із цього кадру
    python scripts/newgen_glyphs.py learn  кадр.png "…" --letters-only
        → лише для назви: літери й цифри назви (окремий розділ `name_digits`);
          еталони цифр, якими читаються дата й час, не чіпати

Назву в `learn` пишіть рівно так, як вона стоїть на екрані поруч із ▶, разом
із `_` і ВЕЛИКИМИ літерами; перенос рядка не пишіть (назва — одним рядком).
Скрипт вимагає, щоб кількість символів збіглась до одного — інакше нічого не
пише, бо еталон не того символу гірший за жоден.

Літери вчаться суворіше за цифри (12.10.26): ще й `_` мусять стояти на кадрі
там, де в назві, «-» і «.» — бути рискою й крапкою за формою, а бітмапа не
сміє вже бути еталоном ІНШОГО символу (так ловиться «0» замість «O» в назві).
Не зійшлось — літери не пишуться (цифри — як і раніше).

`learn` додає варіант лише якщо бітмапа нова, тож повторний запуск безпечний.
`-`, `.` та `_` читач упізнає за формою — їх не вчать.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.machine_newgen_job import (  # noqa: E402
    NEWGEN_GLYPHS_PATH,
    _layout_in_zone,
    _Layout,
    _name_layout,
    _shape_class,
    _underscore_slots,
    read_newgen_name_prefix_explained,
    read_newgen_program_explained,
)


def _layout(frame: Path) -> "_Layout | None":
    """Розклад рядка назви з кадру АБО з уже вирізаного рядка.

    Виріз (`kmill_frame puzzle=N part="zone"`) — це те, що реально доїжджає з
    цеху: сам кадр скринька й MCP віддають зменшеним до 640, а на 640 рядок
    назви не сегментується взагалі. Тому вчити майже завжди доводиться саме з
    вирізу, і скрипт мусить його приймати без окремої команди."""
    image = Image.open(frame)
    layout = _name_layout(image)
    if layout is None or not layout.glyphs:
        layout = _layout_in_zone(image)
    return layout


GLYPHS_FILE = Path(__file__).resolve().parent.parent / NEWGEN_GLYPHS_PATH


def _rows(bits) -> list[str]:
    return ["".join("1" if v else "0" for v in row) for row in bits.tolist()]


def _expected_slots(name: str) -> list[int]:
    """Перед яким символом (без `_`) стоїть кожне `_` назви."""
    slots, count = [], 0
    for char in name:
        if char == "_":
            slots.append(count)
        else:
            count += 1
    return slots


def _letters_problem(layout: "_Layout", name: str) -> "str | None":
    """Чому літери з цього кадру вчити не можна (None — можна)."""
    if any(c.isalpha() and not ("A" <= c <= "Z") for c in name):
        return "літери в назві мусять бути ВЕЛИКИМИ латинськими, як на екрані"
    slots, why = _underscore_slots(layout)
    if slots is None:
        return why
    if slots != _expected_slots(name):
        return f"підкреслення на кадрі перед символами {slots}, у назві — {_expected_slots(name)}"
    for index, (glyph, char) in enumerate(zip(layout.glyphs, name.replace("_", ""))):
        if char in "-." and _shape_class(glyph) != char:
            return f"символ №{index + 1} мав бути «{char}», а за формою це не він"
        if char.isalpha():
            h, _w = glyph.bits.shape
            if h < glyph.cap * 0.9 or glyph.top > glyph.cap * 0.1:
                return f"літера №{index + 1} «{char}» не на всю висоту рядка"
    return None


def learn(frame: Path, name: str, letters_only: bool = False) -> int:
    layout = _layout(frame)
    if layout is None or not layout.glyphs:
        print("Ні рядка ▶ з назвою програми, ні вирізу рядка на цьому файлі не знайдено.")
        return 1
    glyphs = layout.glyphs
    name = name.replace(" ", "")
    truth = name.replace("_", "")
    if len(glyphs) != len(truth):
        print(f"Символів на кадрі {len(glyphs)}, у назві {len(truth)} — не збігається, нічого не пишу.")
        return 1
    data = json.loads(GLYPHS_FILE.read_text(encoding="utf-8")) if GLYPHS_FILE.exists() else {}
    digits = data.setdefault("digits", {})
    letters = data.setdefault("letters", {})
    name_digits = data.setdefault("name_digits", {})

    problem = _letters_problem(layout, name)
    if problem is not None and any(c.isalpha() for c in truth):
        print(f"Літери НЕ вчу: {problem}.")
    # Бітмапа, що вже є еталоном ІНШОГО символу, — майже напевно одруківка в
    # назві («0» замість «O»). Такий кадр не пише нічого.
    known = {tuple(rows): char for section in (digits, name_digits, letters) for char, variants in section.items()
             for rows in variants}
    for index, (glyph, char) in enumerate(zip(glyphs, truth)):
        if not (char.isdigit() or (char.isalpha() and problem is None)):
            continue
        other = known.get(tuple(_rows(glyph.bits)))
        if other is not None and other != char:
            print(f"Символ №{index + 1} «{char}» на кадрі — точнісінько еталон «{other}». "
                  f"Перевірте назву; нічого не пишу.")
            return 1

    added_digits = added_letters = 0
    for glyph, char in zip(glyphs, truth):
        rows = _rows(glyph.bits)
        if char.isdigit() and not letters_only:
            target = digits
        elif char.isdigit() and problem is None:
            # Цифра лише для НАЗВИ: окремий розділ, еталони дати й часу не
            # змінюються. Уже відома хвостові бітмапа сюди не дублюється.
            if rows in digits.get(char, []):
                continue
            target = name_digits
        elif char.isalpha() and problem is None:
            target = letters
        else:
            continue
        variants = target.setdefault(char, [])
        if rows not in variants:
            variants.append(rows)
            if target is letters:
                added_letters += 1
            else:
                added_digits += 1
    data["digits"] = {k: digits[k] for k in sorted(digits)}
    data["name_digits"] = {k: name_digits[k] for k in sorted(name_digits)}
    data["letters"] = {k: letters[k] for k in sorted(letters)}
    GLYPHS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Додано варіантів: цифр {added_digits}, літер {added_letters}. "
          f"Цифри в еталонах: {''.join(sorted(digits))}; літери: {''.join(sorted(letters))}")
    return 0


def check(frame: Path) -> int:
    program, why = read_newgen_program_explained(Image.open(frame))
    if program is None:
        print(f"не прочитано: {why}" if why else "не прочитано: рядка ▶ на кадрі немає (не екран JOBS)")
        return 1
    print(f"дата {program.date} · Sum3D ID {program.sum3d_id} · програма {program.iso_name}")
    prefix, why_not = read_newgen_name_prefix_explained(Image.open(frame))
    if not prefix:
        print(f"початок назви не прочитано: {why_not}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("frame", type=Path)
    p_learn = sub.add_parser("learn")
    p_learn.add_argument("frame", type=Path)
    p_learn.add_argument("name")
    p_learn.add_argument("--letters-only", action="store_true",
                         help="вчити лише назву: літери й цифри назви (name_digits); еталони цифр дати й часу не чіпати")
    args = parser.parse_args()
    if args.cmd == "learn":
        return learn(args.frame, args.name, letters_only=args.letters_only)
    return check(args.frame)


if __name__ == "__main__":
    sys.exit(main())
