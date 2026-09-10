"""Навчити читання назви програми з екрана JOBS верстатів нового покоління.

Два режими над PNG-кадром верстата (такий кадр пише сам застосунок у
machine_frames або в теку калібрування):

    python scripts/newgen_glyphs.py check  кадр.png
        → що ми з нього читаємо (дата й Sum3D ID або «не прочитано»)
    python scripts/newgen_glyphs.py learn  кадр.png "1_18-EMOTIONS-A1-X193_2026-09-04_12-57-22.ISO"
        → додати еталони цифр із цього кадру

Назву в `learn` пишіть рівно так, як вона стоїть на екрані поруч із ▶, разом
із `_`: скрипт сам прибирає підкреслення (у розрізанні їх не видно, вони
лежать нижче базової лінії) і вимагає, щоб кількість символів збіглась до
одного — інакше нічого не пише, бо еталон не того символу гірший за жоден.

`learn` додає варіант лише якщо бітмапа нова, тож повторний запуск безпечний.
Вчаться лише ЦИФРИ: `-`, `.` та `I` читач упізнає за формою.
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
    _name_glyphs,
    read_newgen_program_explained,
)

GLYPHS_FILE = Path(__file__).resolve().parent.parent / NEWGEN_GLYPHS_PATH


def _rows(bits) -> list[str]:
    return ["".join("1" if v else "0" for v in row) for row in bits.tolist()]


def learn(frame: Path, name: str) -> int:
    glyphs = _name_glyphs(Image.open(frame))
    if not glyphs:
        print("На кадрі не знайдено рядка ▶ з назвою програми.")
        return 1
    truth = name.replace("_", "").replace(" ", "")
    if len(glyphs) != len(truth):
        print(f"Символів на кадрі {len(glyphs)}, у назві {len(truth)} — не збігається, нічого не пишу.")
        return 1
    data = json.loads(GLYPHS_FILE.read_text(encoding="utf-8")) if GLYPHS_FILE.exists() else {}
    digits = data.setdefault("digits", {})
    added = 0
    for glyph, char in zip(glyphs, truth):
        if not char.isdigit():
            continue
        rows = _rows(glyph.bits)
        variants = digits.setdefault(char, [])
        if rows not in variants:
            variants.append(rows)
            added += 1
    data["digits"] = {k: digits[k] for k in sorted(digits)}
    GLYPHS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Додано варіантів: {added}. Цифри в еталонах: {''.join(sorted(digits))}")
    return 0


def check(frame: Path) -> int:
    program, why = read_newgen_program_explained(Image.open(frame))
    if program is None:
        print(f"не прочитано: {why}" if why else "не прочитано: рядка ▶ на кадрі немає (не екран JOBS)")
        return 1
    print(f"дата {program.date} · Sum3D ID {program.sum3d_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("frame", type=Path)
    p_learn = sub.add_parser("learn")
    p_learn.add_argument("frame", type=Path)
    p_learn.add_argument("name")
    args = parser.parse_args()
    if args.cmd == "learn":
        return learn(args.frame, args.name)
    return check(args.frame)


if __name__ == "__main__":
    sys.exit(main())
