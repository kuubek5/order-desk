"""Сторож типографічної шкали (V2, частина 1).

Кеглі зведені зі ~26 літеральних значень до 8 токенів `--fs-0..--fs-7`
(tokens.css). Файли зі списку MIGRATED уже переведені — у них не має лишитись
жодного літерального `font-size` (px/rem/em). Список росте пофайлово; додаючи
файл сюди, спершу переведи його через scratchpad/migrate_type.py.

Мета — храповик: раз переведений файл не з'їжджає назад до різнобою.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "css"

# Файли, у яких кеглі вже мають бути лише через токени.
MIGRATED = [
    "v2a_queue.css",
]

# `font-size:` з числовим літералом (px/rem/em). Токени `var(--fs-N)` та
# `var(--size-*)` проходять. Density-фолбек `var(--queue-density-font,13px)`
# теж проходить — там літерал усередині var(), не прямий font-size.
LITERAL_FONT_SIZE = re.compile(r"font-size:\s*\d[\d.]*(?:px|rem|em)\b")


def test_scale_tokens_defined():
    tokens = (CSS_DIR / "tokens.css").read_text(encoding="utf-8")
    for i in range(8):
        assert f"--fs-{i}:" in tokens, f"токен --fs-{i} не оголошено в tokens.css"


@pytest.mark.parametrize("name", MIGRATED)
def test_no_literal_font_size(name: str):
    text = (CSS_DIR / name).read_text(encoding="utf-8")
    hits = LITERAL_FONT_SIZE.findall(text)
    assert not hits, (
        f"{name}: залишились літеральні font-size {sorted(set(hits))} — "
        f"переведи їх на токени шкали (--fs-0..--fs-7)"
    )
