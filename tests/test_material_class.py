from app.material_class import material_color_css_class


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
