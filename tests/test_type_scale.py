"""Сторож типографічної шкали (V2).

Кеглі зведені зі ~26 літеральних значень до 8 токенів `--fs-0..--fs-7`
(tokens.css). Правило-храповик на весь каталог CSS: жодного літерального
`font-size` у «тісному» діапазоні (≤ 22.5px) — там і був різнобій, він має йти
через токени. Дозволено:
  * `var(--fs-N)` / `var(--size-*)` — токени;
  * великі дисплейні літерали (> 22.5px) — h1/герої, це навмисна ієрархія;
  * `em` — відносний до батька, конвертувати в фікс не можна;
  * літерал усередині `var(--x, 13px)` — це фолбек, не прямий font-size.

Мігрувати новий файл: scratchpad/migrate_type2.py (px/rem ≤ 22.5 → найближчий
токен, нічия — вгору).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "css"
CSS_FILES = sorted(p.name for p in CSS_DIR.glob("*.css"))

# Прямий `font-size:` з числовим літералом px/rem (не всередині var()).
LITERAL = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)(px|rem)(?![\w.])")
SMALL_MAX_PX = 22.5


def _small_literals(text: str) -> list[str]:
    out = []
    for m in LITERAL.finditer(text):
        px = float(m.group(1)) * (1.0 if m.group(2) == "px" else 16.0)
        if px <= SMALL_MAX_PX:
            out.append(f"{m.group(1)}{m.group(2)}")
    return out


def test_scale_tokens_defined():
    tokens = (CSS_DIR / "tokens.css").read_text(encoding="utf-8")
    for i in range(8):
        assert f"--fs-{i}:" in tokens, f"токен --fs-{i} не оголошено в tokens.css"


@pytest.mark.parametrize("name", CSS_FILES)
def test_no_small_literal_font_size(name: str):
    text = (CSS_DIR / name).read_text(encoding="utf-8")
    hits = _small_literals(text)
    assert not hits, (
        f"{name}: літеральні кеглі {sorted(set(hits))} у діапазоні шкали — "
        f"переведи на токени (--fs-0..--fs-7), напр. через scratchpad/migrate_type2.py"
    )
