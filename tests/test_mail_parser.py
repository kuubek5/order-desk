from app.mail_parser import guess_fields_from_text


def test_extracts_material_color():
    result = guess_fields_from_text("Добрий день, колір: моно А3.5, дякую")
    assert result["material_color_guess"] == "моно А3.5"


def test_extracts_quantity():
    result = guess_fields_from_text("кількість: 4 шт")
    assert result["quantity_guess"] == "4"


def test_extracts_kind():
    result = guess_fields_from_text("на фрезерування: абатмент")
    assert result["kind_guess"] == "абатмент"


def test_missing_fields_are_none():
    result = guess_fields_from_text("просто текст без підказок")
    assert result["material_color_guess"] is None
    assert result["kind_guess"] is None
    assert result["quantity_guess"] is None


def test_case_insensitive():
    result = guess_fields_from_text("КОЛІР: пмма A2")
    assert result["material_color_guess"] == "пмма A2"
