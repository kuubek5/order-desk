from types import SimpleNamespace

from app.material_class import (
    mail_material_badge,
    material_badge,
    material_color_css_class,
)


def _order(material_name=None, is_production=True, material_color="моно а3"):
    material = None if material_name is None else SimpleNamespace(
        name=material_name, is_production=is_production
    )
    return SimpleNamespace(material=material, material_color=material_color)


class TestMaterialBadge:
    def test_zircon_symbol_and_class(self):
        b = material_badge(_order("Цирконій"))
        assert b == {"symbol": "Zr", "cls": "mat-zr", "title": "Цирконій"}

    def test_each_production_material_maps(self):
        cases = {
            "ПММА": ("PMMA", "mat-pmma"),
            "Титан": ("Ti", "mat-ti"),
            "СЛМ": ("SLM", "mat-slm"),
            "Віск": ("Wax", "mat-wax"),
        }
        for name, (sym, cls) in cases.items():
            b = material_badge(_order(name))
            assert (b["symbol"], b["cls"]) == (sym, cls)

    def test_non_material_bucket_has_no_badge(self):
        assert material_badge(_order("Не матеріал", is_production=False)) is None

    def test_unresolved_colour_shows_question_mark(self):
        b = material_badge(_order(None, material_color="загадка"))
        assert b["symbol"] == "?" and b["cls"] == "mat-unknown"

    def test_no_colour_shows_no_badge(self):
        assert material_badge(_order(None, material_color="")) is None
        assert material_badge(_order(None, material_color=None)) is None

    def test_unknown_material_name_falls_back(self):
        b = material_badge(_order("Скло"))
        assert b["cls"] == "mat-other" and b["symbol"] == "Скло"


def test_titan_at_start():
    assert material_color_css_class("титан корея") == "chip-titan"


def test_titan_at_end():
    assert material_color_css_class("с2 транс титан") == "chip-titan"


def test_titan_case_insensitive():
    assert material_color_css_class("ТИТАН корея") == "chip-titan"


def test_pmma_highlighted():
    assert material_color_css_class("пмма A2") == "chip-pmma"


def test_zircon_variants_unchanged():
    assert material_color_css_class("моно А3.5") == ""
    assert material_color_css_class("емо а3") == ""
    assert material_color_css_class("800") == ""
    assert material_color_css_class("тисячний") == ""


def test_none_and_empty():
    assert material_color_css_class(None) == ""
    assert material_color_css_class("") == ""


# ── Чіп матеріалу+кольору на рядку тріажу (mail_material_badge) ───────────────


def test_mail_badge_recognises_zircon_synonyms_and_typos():
    # Виробник monolith; клієнти пишуть по-різному — сидові аліаси + нечіткий збіг.
    for text in ("monolith a2", "монолайт a2", "моно а3", "моноліт а3.5", "800"):
        b = mail_material_badge(text)
        assert b is not None and b["symbol"] == "Zr" and b["cls"] == "mat-zr"


def test_mail_badge_pulls_shade_code():
    assert mail_material_badge("monolith a2")["color"] == "a2"
    assert mail_material_badge("pmma a2") == {
        "symbol": "PMMA", "cls": "mat-pmma", "title": "ПММА", "color": "a2",
    }
    # Матеріал без коду відтінку — чіп є, колір порожній.
    assert mail_material_badge("титан корея")["symbol"] == "Ti"
    assert mail_material_badge("титан корея")["color"] == ""


def test_mail_badge_hidden_for_non_material_and_unknown():
    # Не наша робота, омоніми й порожнеча — БЕЗ чіпа (не вигадуємо).
    for text in (None, "", "моделювання втулки", "implant abatment", "нет времени"):
        assert mail_material_badge(text) is None


def test_mail_badge_respects_db_aliases():
    # Адмін довчив написання в бібліотеці матеріалів → чіп його підхоплює.
    from app.material_classifier import seed_alias_rows, AliasRow, PMMA
    # Нейтральне написання (не містить сидових аліасів на кшталт «моно»), яке
    # без запису в БАЗІ не впізнати.
    novel = "флексикор b1"
    assert mail_material_badge(novel) is None  # без аліаса — не гадаємо
    extended = seed_alias_rows() + [AliasRow(pattern="флексикор", match_type="contains", material=PMMA)]
    b = mail_material_badge(novel, extended)
    assert b is not None and b["symbol"] == "PMMA"
