"""Підказка кольорів багатокольорового листа (app/mail_color_split.py).

Бойовий лист 25.09.26: пересланий від cadcamlab, текст «Monolith кольору денис,
юшков а3.5, роман а3, лідія а 1», дев'ять файлів «16.09.2026-Проект <пацієнт>».
"""

from types import SimpleNamespace

from app.mail_color_split import material_base, parse_name_shades, suggest_color_plan

BODY = """--- Повідомлення, що пересилається ---
Від кого: Iris <irisdent.ua@gmail.com>
Кому: <cadcamlab@ukr.net>
Тема: .
Дата: 17 вересня 2026, 00:01:05

Monolith кольору денис, юшков а3.5, роман а3, лідія а 1
Данні для відправки: Грунський Сергій Миколайович 0683992978
Нова пошта:5 м.Фастів
Для зв'язку при технічних проблемах чи недостачі файлів прошу скористатись цим номером +0677783434

--
Ваш найбільший CAD CAM Центр в Україні «Стаханівець»
тел. (095) 435 20 08; (067) 407 69 07
"""


def _att(i, name):
    return SimpleNamespace(id=i, filename=name)


FILES = [
    _att(1, "16.09.2026-Проект денис_2026-09-16_15-10.constructionInfo"),
    _att(2, "16.09.2026-Проект денис_2026-09-16_15-10.stl"),
    _att(3, "16.09.2026-Проект людмила_2026-09-16_15-20.constructionInfo"),
    _att(4, "16.09.2026-Проект людмила_2026-09-16_15-20.stl"),
    _att(5, "16.09.2026-Проект роман_2026-09-16_15-30.constructionInfo"),
    _att(6, "16.09.2026-Проект роман_2026-09-16_15-30.stl"),
    _att(7, "16.09.2026-Проект юшков_2026-09-16_15-40.constructionInfo"),
    _att(8, "16.09.2026-Проект юшков_2026-09-16_15-40.stl"),
    _att(9, "16.09.2026-Проект лідія_2026-09-16_15-50.stl"),
]


def test_names_before_a_shade_take_that_shade():
    pairs = parse_name_shades(BODY, {"monolith"})
    assert pairs == {"денис": "a3.5", "юшков": "a3.5", "роман": "a3", "лідія": "a1"}


def test_delivery_details_and_dates_get_no_shade():
    # «Грунський Сергій» без кольору після себе, «вересня 2026» — не відтінок.
    pairs = parse_name_shades(BODY, {"monolith"})
    assert "грунський" not in pairs and "вересня" not in pairs


def test_real_letter_splits_into_three_colours():
    plan = suggest_color_plan(BODY, "Monolith a3.5", FILES)
    assert plan is not None
    got = {g.shade: sorted(g.attachment_ids) for g in plan.groups}
    assert got == {"a3.5": [1, 2, 7, 8], "a3": [5, 6], "a1": [9]}
    # Порядок груп — як кольори в тексті.
    assert [g.shade for g in plan.groups] == ["a3.5", "a3", "a1"]
    assert plan.groups[0].material == "monolith a3.5"
    assert set(plan.groups[0].names) == {"денис", "юшков"}


def test_file_of_unnamed_patient_is_not_guessed():
    # «людмила» в тексті немає («лідія» — інша людина): файл лишається «?».
    plan = suggest_color_plan(BODY, "Monolith a3.5", FILES)
    assert plan is not None
    assert sorted(plan.unmatched_ids) == [3, 4]
    assert 3 not in plan.by_attachment


def test_latin_filename_matches_cyrillic_name():
    files = [_att(1, "Project_denys.stl"), _att(2, "roman_upper.stl")]
    plan = suggest_color_plan("денис а3.5, роман а3", "mono", files)
    assert plan is not None
    assert plan.by_attachment == {1: "a3.5", 2: "a3"}


def test_real_filenames_of_the_letter():
    # Справжні імена з листа 25.09.26: «деніс» (у тексті «денис»), «люда
    # італія» (у тексті «лідія» — інша людина), «юшков вч» із мостами.
    files = [
        _att(1, "16.09.2026-Проєкт деніс цр.constructionInfo"),
        _att(2, "16.09.2026-Проєкт деніс цр-35-crown_cad.stl"),
        _att(3, "16.09.2026-Проєкт люда італія цр.constructionInfo"),
        _att(4, "16.09.2026-Проєкт люда італія цр-36-crown_cad.stl"),
        _att(5, "16.09.2026-Проєкт роман цр.constructionInfo"),
        _att(6, "16.09.2026-Проєкт роман цр-46-crown_cad.stl"),
        _att(7, "16.09.2026-Проєкт юшков вч цр.constructionInfo"),
        _att(8, "16.09.2026-Проєкт юшков вч цр-24-25-bridge_cad.stl"),
        _att(9, "16.09.2026-Проєкт юшков вч цр-17-16-15-bridge_cad.stl"),
    ]
    plan = suggest_color_plan(BODY, "Monolith", files)
    assert plan is not None
    got = {g.shade: sorted(g.attachment_ids) for g in plan.groups}
    assert got == {"a3.5": [1, 2, 7, 8, 9], "a3": [5, 6]}
    assert sorted(plan.unmatched_ids) == [3, 4]


def test_url_noise_does_not_create_shades():
    body = "роман а3\nhttps://files.ukr.net/x?token=Ab-a2-Cd денис\nюшков а1"
    pairs = parse_name_shades(body, set())
    assert pairs == {"роман": "a3", "юшков": "a1"}


def test_transliteration_variants_are_one_person():
    files = [_att(1, "serhii.stl"), _att(2, "iushkov.stl")]
    plan = suggest_color_plan("сергій а2, юшков а3", "", files)
    assert plan is not None
    assert plan.by_attachment == {1: "a2", 2: "a3"}


def test_similar_but_different_people_are_not_merged():
    # Однакова «схожість» із denis/denys, але це інші люди — не вгадуємо.
    files = [_att(1, "ірена.stl"), _att(2, "іванна.stl"), _att(3, "роман.stl")]
    plan = suggest_color_plan("ірина а1, іван а2, роман а3", "", files)
    assert plan is not None
    assert plan.by_attachment == {3: "a3"}
    assert sorted(plan.unmatched_ids) == [1, 2]


def test_remainder_after_first_batch_still_gets_a_hint():
    # Партію a3 (роман) уже прийнято: у листі лишились a3.5 і «?». Блок не
    # мусить зникати лише тому, що колір тепер один.
    files = [
        _att(1, "16.09.2026-Проєкт деніс цр-35-crown_cad.stl"),
        _att(3, "16.09.2026-Проєкт люда італія цр-36-crown_cad.stl"),
        _att(8, "16.09.2026-Проєкт юшков вч цр-24-25-bridge_cad.stl"),
    ]
    plan = suggest_color_plan(BODY, "Monolith", files)
    assert plan is not None
    assert [(g.shade, g.attachment_ids) for g in plan.groups] == [("a3.5", [1, 8])]
    assert plan.unmatched_ids == [3]


def test_single_colour_letter_gives_no_plan():
    files = [_att(1, "денис.stl"), _att(2, "роман.stl")]
    assert suggest_color_plan("денис а3, роман а3", "mono a3", files) is None


def test_no_names_in_files_gives_no_plan():
    files = [_att(1, "scan_upper.stl"), _att(2, "scan_lower.stl")]
    assert suggest_color_plan(BODY, "Monolith", files) is None


def test_material_word_is_not_a_patient():
    # Без здогаду матеріалу «monolith» стояв би перед a3.5 як «пацієнт».
    pairs = parse_name_shades("Monolith денис а3.5, роман а3", {"monolith"})
    assert "monolith" not in pairs


def test_material_base_strips_shade():
    assert material_base("Monolith a3.5") == "monolith"
    assert material_base("моно А3") == "моно"
    assert material_base(None) == ""
