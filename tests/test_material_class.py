from types import SimpleNamespace

from app.material_class import material_badge, material_color_css_class


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
