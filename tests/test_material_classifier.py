"""app/material_classifier.py — classification of the real, messy
"Колір роботи" values Roman supplied (zircon lines, PMMA family, SLM, titanium,
non-material stage/part rows, and the typos the sheet is full of)."""

import pytest

from app.material_classifier import (
    NON_MATERIAL,
    PMMA,
    SLM,
    TITANIUM,
    WAX,
    ZIRCON,
    classify_material,
    normalize_material,
)


@pytest.mark.parametrize(
    "raw",
    [
        "800", "500", "1000", "2000", "1333",       # manufacturer colour codes
        "моно а3", "моно А2", "моно а3.5", "моно -А2",
        "моноліт а 3", "моноліт а 3,5", "моноліт с 3",
        "mono a3", "mono a35", "mono c3", "mono BL2", "mono d2",
        "емо А2", "емо а3", "emo a1", "emo c3", "emo B1",
        "утмл а2", "stml a2", "вівід стмл а2",
        "z nat a3", "z nat a3.5", "nature", "katana", "циркон",
    ],
)
def test_zircon_variants(raw):
    assert classify_material(raw) == ZIRCON


@pytest.mark.parametrize(
    "raw",
    ["монліт а 3", "миноліт а3", "моноа а 3,5"],
)
def test_zircon_typos_via_fuzzy(raw):
    assert classify_material(raw) == ZIRCON


@pytest.mark.parametrize(
    "raw",
    ["пмма А2", "ПММА А2", "pmma a3", "pmma a3.5", "пмма прозора",
     "хіпс а 2", "kappa", "каппа"],
)
def test_pmma_family(raw):
    assert classify_material(raw) == PMMA


@pytest.mark.parametrize("raw", ["слм", "slm"])
def test_slm(raw):
    assert classify_material(raw) == SLM


@pytest.mark.parametrize("raw", ["титан корея", "Ti", "titan", "tit"])
def test_titanium(raw):
    assert classify_material(raw) == TITANIUM


@pytest.mark.parametrize("raw", ["wax", "віск", "воск"])
def test_wax(raw):
    assert classify_material(raw) == WAX


@pytest.mark.parametrize(
    "raw",
    # translucency markers + bare-shade fallback all default to zirconia
    ["st a2", "st a1", "s1", "a3 tr", "a2 транс", "с2 транс", "с 3 транс", "a2", "а3.5", "bl2"],
)
def test_zircon_translucency_and_bare_shade_fallback(raw):
    assert classify_material(raw) == ZIRCON


@pytest.mark.parametrize("raw", ["hipc a2", "hipc a3", "trinia"])
def test_pmma_extra_variants(raw):
    assert classify_material(raw) == PMMA


@pytest.mark.parametrize("raw", ["vtulka", "анатомія А 3,5"])
def test_non_material_extra_variants(raw):
    assert classify_material(raw) == NON_MATERIAL


def test_pmma_shade_is_not_overridden_by_zircon_fallback():
    # The shade fallback must never fire when a material word is present — a
    # bare 'a2' is zirconia, but 'пмма a2' stays PMMA.
    assert classify_material("пмма a2") == PMMA
    assert classify_material("pmma a3.5") == PMMA


@pytest.mark.parametrize("raw", ["моделювання", "втулка", "implant", "імплант"])
def test_non_material_rows(raw):
    assert classify_material(raw) == NON_MATERIAL


@pytest.mark.parametrize("raw", ["", "   ", None, "???", "12", "невідоме слово"])
def test_unresolved_returns_none(raw):
    # Empty/garbage, a bare number (not a colour code), or an unknown word can't
    # be pinned to a material — must stay unresolved (None), never guessed.
    # (Bare tooth shades like a2 DO resolve to zirconia by lab convention — see
    # test_zircon_translucency_and_bare_shade_fallback.)
    assert classify_material(raw) is None


def test_ti_token_does_not_match_inside_words():
    # 'ti' is a token alias; it must not fire on a substring like the 'ti' in
    # a longer word. 'multi' contains 'ti' but is not titanium.
    assert classify_material("multi") != TITANIUM


def test_normalize_unifies_comma_case_and_spaces():
    assert normalize_material("Моноліт А 3,5") == "моноліт а 3.5"
    assert normalize_material("  PMMA   A2 ") == "pmma a2"
