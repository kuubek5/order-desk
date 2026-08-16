from app.mail_parser import fuzzy_match_material_color, guess_fields_from_text, guess_service_type


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


def test_dash_separated_color():
    result = guess_fields_from_text("Колір - А2; Кількість - 4")
    assert result["material_color_guess"] == "А2"


def test_dash_separated_quantity():
    result = guess_fields_from_text("Кількість - 4")
    assert result["quantity_guess"] == "4"


def test_dash_separated_quantity_no_space_after_number():
    result = guess_fields_from_text("Просимо виготовити коронку, кількість - 2шт.")
    assert result["quantity_guess"] == "2"


def test_bare_subject_used_as_material_color_fallback():
    """Real client email: subject 'моно а3', empty body, no keywords at all."""
    result = guess_fields_from_text("моно а3\n", subject="моно а3")
    assert result["material_color_guess"] == "моно а3"


def test_long_subject_not_used_as_material_color_fallback():
    result = guess_fields_from_text(
        "Просимо прорахувати роботу якнайшвидше, дякуємо\n",
        subject="Просимо прорахувати роботу якнайшвидше, дякуємо",
    )
    assert result["material_color_guess"] is None


def test_keyword_match_takes_priority_over_subject_fallback():
    result = guess_fields_from_text("колір: пмма A2\n", subject="нове замовлення")
    assert result["material_color_guess"] == "пмма A2"


def test_bare_body_used_as_fallback_when_subject_empty():
    """Real client email: empty subject, body 'емаушан а4', no keywords."""
    result = guess_fields_from_text("\nемаушан а4\r\n", subject="", body="емаушан а4\r\n")
    assert result["material_color_guess"] == "емаушан а4"


def test_subject_fallback_takes_priority_over_body_fallback():
    result = guess_fields_from_text("моно а3\nпмма а2", subject="моно а3", body="пмма а2")
    assert result["material_color_guess"] == "моно а3"


# --- fuzzy_match_material_color -------------------------------------------------


def test_fuzzy_match_material_color_accepts_close_typo():
    known = ["моно А3.5", "пмма A2", "титан корея"]
    assert fuzzy_match_material_color("моно а3.5.", known) == "моно А3.5"


def test_fuzzy_match_material_color_rejects_made_up_word():
    known = ["моно А3.5", "пмма A2", "титан корея", "500", "800"]
    assert fuzzy_match_material_color("емаушан а4", known) is None


def test_fuzzy_match_material_color_no_known_materials_returns_none():
    assert fuzzy_match_material_color("моно а3", []) is None
    assert fuzzy_match_material_color("моно а3", None) is None


def test_fuzzy_match_material_color_latin_subject_maps_to_cyrillic():
    """Clients write email subjects in Latin ('emo a3.5', 'pmma a2') but the
    sheet's materials are Cyrillic ('емо а3.5', 'пмма а2'). Transliteration
    must bridge the two alphabets — a real gap found on the live ukr.net box."""
    known = ["емо а3.5", "пмма а2", "емо а2", "емо а3", "моно а3.5"]
    assert fuzzy_match_material_color("emo a3.5", known) == "емо а3.5"
    assert fuzzy_match_material_color("pmma a2", known) == "пмма а2"
    # colour digits stay distinct: 'emo a2' must not grab 'емо а3'
    assert fuzzy_match_material_color("emo a2", known) == "емо а2"


def test_fuzzy_match_material_color_latin_garbage_still_rejected():
    """Transliteration must not turn nonsense into a false positive."""
    known = ["емо а3.5", "пмма а2", "500", "800"]
    assert fuzzy_match_material_color("xyzzy", known) is None


# --- guess_fields_from_text + known_materials integration -----------------------


def test_bare_subject_fallback_maps_typo_to_known_material():
    """Client typo 'моно а3 ' (trailing space/dot) should map to the sheet's
    canonical 'моно А3.5' when that's the closest known reference value."""
    known_materials = ["моно А3.5", "пмма A2", "титан корея"]
    result = guess_fields_from_text(
        "моно а3.5.\n", subject="моно а3.5.", known_materials=known_materials
    )
    assert result["material_color_guess"] == "моно А3.5"


def test_bare_body_fallback_made_up_word_not_accepted_with_known_materials():
    """The 'емаушан а4' test email from the user: a made-up word that isn't
    close to anything real should NOT be accepted as material_color_guess
    when a reference list is supplied."""
    known_materials = ["моно А3.5", "пмма A2", "титан корея", "500", "800"]
    result = guess_fields_from_text(
        "\nемаушан а4\r\n",
        subject="",
        body="емаушан а4\r\n",
        known_materials=known_materials,
    )
    assert result["material_color_guess"] is None


def test_bare_fallback_without_known_materials_keeps_old_behaviour():
    """No known_materials passed at all (None, the default) — regression
    guard: existing behaviour of accepting the short phrase as-is must not
    change, since existing callers/tests don't pass this argument."""
    result = guess_fields_from_text("емаушан а4\n", subject="емаушан а4")
    assert result["material_color_guess"] == "емаушан а4"


def test_bare_fallback_with_empty_known_materials_keeps_old_behaviour():
    """Explicit empty list behaves the same as None (old behaviour)."""
    result = guess_fields_from_text(
        "емаушан а4\n", subject="емаушан а4", known_materials=[]
    )
    assert result["material_color_guess"] == "емаушан а4"


def test_keyword_takes_priority_over_fuzzy_fallback_regardless_of_known_materials():
    """A keyword match ('колір:') must win even when known_materials is
    supplied and even when the keyword's own value wouldn't fuzzy-match
    anything in known_materials."""
    known_materials = ["зовсім інша назва"]
    result = guess_fields_from_text(
        "колір: пмма A2\n", subject="нове замовлення", known_materials=known_materials
    )
    assert result["material_color_guess"] == "пмма A2"


# --- guess_service_type -----------------------------------------------------
#
# This is a display-only hint for the mail triage screen (CLAUDE.md screen
# 2), never a filter that removes a message from the list — see the
# function's docstring. Tests below only cover the return value, not any
# hiding behaviour, because there is none to test: guess_service_type never
# sees the full email list, only free text.


def test_guess_service_type_detects_3d_print_ukrainian():
    assert guess_service_type("Добрий день, потрібен 3D друк моделі щелепи") == "3d_print"


def test_guess_service_type_detects_3d_print_surzhyk_short():
    assert guess_service_type("3д друк, дякую") == "3d_print"


def test_guess_service_type_detects_hyphenated_and_verb_form():
    assert guess_service_type("Прошу надрукувати модель, файл додаю") == "3d_print"


def test_guess_service_type_detects_english_phrasing():
    assert guess_service_type("Hi, could you do 3D printing of these models?") == "3d_print"


def test_guess_service_type_milling_email_returns_none():
    """A normal milling order with no mention of printing at all — the
    default assumption for this mailbox, so None (not a milling-specific
    string) is the expected 'no signal' result."""
    result = guess_service_type(
        "Добрий день, на фрезерування: колір пмма A2, кількість: 3 шт, дякую"
    )
    assert result is None


def test_guess_service_type_unrelated_text_returns_none():
    assert guess_service_type("Дзвоніть, будь ласка, після обіду") is None


def test_guess_service_type_empty_text_returns_none():
    assert guess_service_type("") is None
    assert guess_service_type(None) is None


def test_guess_service_type_mixed_milling_and_printing_flags_as_3d_print():
    """Decision: when a message mentions BOTH milling and 3D printing, we
    still flag it as '3d_print' rather than None/milling. This is a
    deliberate choice, not an oversight — see the docstring. An operator
    double-checking a mixed email that turns out to be milling-only costs
    nothing (this is only a visual hint, the email stays in the list either
    way); silently treating a mixed request as pure milling could mean the
    3D-print half of the request gets missed entirely."""
    mixed_text = (
        "Добрий день, на фрезерування: колір пмма A2, кількість 3 шт. "
        "Також окремо потрібен 3d друк моделі щелепи."
    )
    assert guess_service_type(mixed_text) == "3d_print"


# --- material_candidates (accept wizard chips) ---------------------------------

def test_material_candidates_latin_returns_ranked_cyrillic():
    from app.mail_parser import material_candidates
    known = ["емо а3.5", "емо а3", "емо а2", "моно а3", "пмма а2"]
    out = material_candidates("emo a3", known, limit=3)
    assert out and out[0] == "емо а3"  # exact translit match ranks first
    assert len(out) <= 3


def test_material_candidates_garbage_returns_empty():
    from app.mail_parser import material_candidates
    assert material_candidates("zzzzz", ["емо а3", "пмма а2"]) == []


def test_material_candidates_empty_inputs():
    from app.mail_parser import material_candidates
    assert material_candidates("", ["емо а3"]) == []
    assert material_candidates("емо", []) == []
