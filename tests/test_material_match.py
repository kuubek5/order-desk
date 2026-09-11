"""Зіставлення «матеріал у таблиці ↔ назва теки в export».

Назви тут не вигадані — це реальний випадок з прода 28.08.26 (клієнт
Pavlenko): у таблиці стояло `emo a3` і `emo a1`, на диску лежали
`Emotions A3 опаковий всередині` та `Emotions A 1 опаковий всередині`, і
робота на видачі не знаходилась, хоча тека була поруч.
"""

import pytest

from app.material_match import materials_match, shades, words


PAVLENKO_FOLDERS = [
    "Emotions A 3,5",
    "Emotions A3",
    "emo a2",
    "emo a3.5",
    "Emotions A2",
    "Emotions A 1 опаковий всередині",
    "Emotions A3 опаковий всередині",
    "Віск. Відправити Клавдіїв В., Київ, Нова пошта 71",
]


class TestTheProductionCase:
    def test_short_sheet_name_matches_the_full_folder_name(self):
        assert materials_match("emo a3", "Emotions A3 опаковий всередині")

    def test_the_other_pavlenko_row_matches_its_own_folder(self):
        assert materials_match("emo a1", "Emotions A 1 опаковий всередині")

    @pytest.mark.parametrize("folder", PAVLENKO_FOLDERS)
    def test_emo_a3_takes_only_its_own_shade(self, folder):
        expected = folder in ("Emotions A3", "Emotions A3 опаковий всередині")
        assert materials_match("emo a3", folder) is expected


class TestShadeDecides:
    def test_half_shade_is_not_the_whole_shade(self):
        assert not materials_match("emo a3", "Emotions A 3,5")
        assert not materials_match("emo a3.5", "Emotions A3")

    def test_comma_and_dot_are_the_same_shade(self):
        assert materials_match("mono a3,5", "mono a3.5")

    def test_a_detached_digit_still_belongs_to_its_letter(self):
        assert shades("Emotions A 3,5") == {"a3.5"}
        assert shades("emo a3") == {"a3"}

    def test_manufacturer_colour_codes_count_as_shades(self):
        # CLAUDE.md §3: `500` = A1 опак, `800` = A2 опак — це кольори.
        assert shades("mono 500") == {"500"}
        assert materials_match("mono 500", "Monolith 500")
        assert not materials_match("mono 500", "Monolith 800")

    def test_a_folder_without_a_shade_cannot_confirm_a_shaded_row(self):
        assert not materials_match("emo a3", "Emotions")


class TestMaterialLinesStaySeparate:
    def test_different_lines_of_the_same_zirconia_do_not_merge(self):
        # Обидва — цирконій відтінку A3, але це фізично різні диски.
        assert not materials_match("mono a3", "Emotions A3")
        assert not materials_match("emo a3", "Monolith A3")

    def test_pmma_does_not_take_a_zirconia_folder(self):
        assert not materials_match("pmma a3", "Emotions A3")

    def test_a_short_code_is_not_a_prefix_of_anything(self):
        # `st` не мусить хапати `stomatology`, `s1` — `s1000`.
        assert not materials_match("st a2", "Stomatology A2")


class TestFolderExtras:
    def test_technician_notes_in_the_folder_name_do_not_block_a_match(self):
        assert materials_match("emo a2", "emo a2 опаковий всередині")

    def test_noise_words_alone_never_create_a_match(self):
        assert not materials_match("опаковий", "Emotions A3 опаковий всередині")

    def test_extra_words_on_the_SHEET_side_do_block_it(self):
        # Технік написав більше, ніж є в теці — це вже інша робота.
        assert not materials_match("emo a3 гвинтова", "Emotions A3")

    def test_an_empty_material_matches_nothing(self):
        assert not materials_match("", "Emotions A3")
        assert not materials_match(None, "Emotions A3")


class TestWordsAndShadesSplit:
    def test_words_drop_shades_and_noise(self):
        assert words("Emotions A3 опаковий всередині") == {"emotions"}

    def test_a_row_without_a_shade_matches_on_the_name_alone(self):
        assert materials_match("тит", "Титан")
        assert not materials_match("тит", "Емоушн")


class TestFolderNamesTheKindOfWorkNotTheMaterial:
    """Бойовий випадок 10.09.26 (Oleksandr): партія за 10.09 мала теку
    «Повна анатомія колір А3» (кирилична «А»), рядок таблиці — `mono a3`.
    Тека не збігалась, і видача відкривала партію за 09.09."""

    def test_work_kind_and_shade_only_folder_matches_on_the_shade(self):
        assert materials_match("mono a3", "Повна анатомія колір А3")
        assert materials_match("emo a2", "повна анатомія колір A2")

    def test_the_shade_still_decides(self):
        assert not materials_match("mono a3", "Повна анатомія колір А3.5")
        assert not materials_match("mono a2", "Повна анатомія колір А3")

    def test_a_folder_that_names_another_material_still_blocks(self):
        assert not materials_match("mono a3", "Emotions A3 повна анатомія")

    def test_work_kind_words_alone_never_create_a_match(self):
        assert not materials_match("mono", "Повна анатомія")

    def test_a_shade_only_folder_is_a_candidate_by_shade(self):
        """Тека «А3» (11.09.26: ~10 таких на диску) матеріалу не заперечує —
        кандидат за відтінком, як і «Повна анатомія колір А3»."""
        assert materials_match("mono a3", "A3")
        assert not materials_match("mono a3", "A2")


class TestCyrillicShadeLetter:
    def test_cyrillic_a_is_the_same_shade_as_latin(self):
        assert materials_match("mono a3", "Monolith А3")
        assert materials_match("mono c2", "mono С2")
        assert not materials_match("mono a3", "Monolith В3")


class TestCyrillicMaterialName:
    """Бойовий випадок 11.09.26 (Лагус): рядок `emo a2`, тека «Циркон емоушен
    a2» — латинське `emo` не є початком кириличного `емоушен`, і видача теку
    не бачила, хоча вона лежала за той самий день."""

    def test_cyrillic_folder_name_matches_latin_sheet(self):
        assert materials_match("emo a2", "Циркон емоушен a2")
        assert materials_match("mono a3", "Моно А3")
        assert materials_match("pmma a2", "ПММА A2")

    def test_latin_sheet_cyrillic_folder_still_checks_shade_and_line(self):
        assert not materials_match("emo a2", "Циркон емоушен a3")
        assert not materials_match("mono a2", "Циркон емоушен a2")
        assert not materials_match("emo a2", "Моно A2")


class TestHowPeopleNameTheFolder:
    """Бойовий випадок 11.09.26 (Shehera): рядок `kappa`, теки за день —
    «Фрезернути капу» і «Фрезернути капу SEC». Кирилиця, одна «п» замість
    двох і відмінок («капу», не «капа») — три розбіжності в одному слові."""

    def test_inflected_cyrillic_single_consonant_matches(self):
        assert materials_match("kappa", "Фрезернути капу")
        assert materials_match("kappa", "Фрезернути капу SEC")
        assert materials_match("kappa", "kappa")

    def test_ending_rule_is_equality_of_stem_not_prefix(self):
        assert not materials_match("mono a3", "Монтаж A3")
        assert not materials_match("kappa", "Капсула")
        assert not materials_match("kappa", "Каркас")
        assert not materials_match("тит", "Емоушн")

    def test_shade_glued_to_the_word_still_decides(self):
        assert not materials_match("монос3", "mono a3")
        assert materials_match("монос3", "mono C3")
        assert materials_match("emoa3", "Emotions A3")



class TestRealFolderNames2026_09_11:
    """Словник із реальних назв тек за 60 днів (список з робочого ПК,
    11.09.26: 2540 тек, 514 назв). Імена клієнтів тут вигадані."""

    def test_a35_is_a3_5_on_both_sides(self):
        assert materials_match("mono a35", "mono a3.5")
        assert materials_match("mono a3.5", "mono a35")
        assert materials_match("emo a3,5", "monoA35") is False
        assert materials_match("mono a3,5", "monoA35")
        assert not materials_match("mono a35", "mono a3")

    def test_shade_written_with_hyphen_or_underscore(self):
        assert materials_match("mono a3", "Monolith А-3 (Іваненко 46з)")
        assert materials_match("mono a3.5", "monolith - A-3.5")
        assert materials_match("mono a3.5", "A3_5 моноліт")
        assert materials_match("emo c3", "С-3 Emotions")
        assert materials_match("emo b2", "В-2 Emotions")
        assert materials_match("mono a3", "Моноліт-глазурь-А-3,")

    def test_cyrillic_p_that_looks_latin(self):
        assert materials_match("pmma a3", "РММА А3")
        assert materials_match("pmma a2", "Іваненко РММА А2")

    def test_bleach_shades(self):
        assert materials_match("моно бліч 2", "mono bl2")
        assert materials_match("emo bl4", "Циркон емоушен блич 4")
        assert materials_match("emo bl2", "Emotions bleach 2")
        assert not materials_match("mono bl2", "mono bl3")
        assert not materials_match("mono a2", "mono bl2")

    def test_cyrillic_b_as_shade_b(self):
        assert materials_match("emo b2", "Циркон емоушен б2")

    def test_titanium_wax_and_splint_families_without_shade(self):
        assert materials_match("титан корея", "tit")
        assert materials_match("tit", "Титан з анодуванням")
        assert materials_match("tit", "ТІТ")
        assert materials_match("tit", "t i t")
        assert materials_match("wax", "воск")
        assert materials_match("віск", "Віск. Відправити")
        assert materials_match("kappa", "Іваненко Сплінт")
        assert materials_match("kappa", "Фрезернути капу2")
        assert not materials_match("tit", "wax")
        assert not materials_match("kappa", "tit")

    def test_translucent(self):
        assert materials_match("с2 транс", "c2tr")
        assert materials_match("а2 транс", "a2 tr")
        assert not materials_match("с2 транс", "c3tr")

    def test_generic_zirconia_folder_matches_only_zirconia_work(self):
        assert materials_match("mono a4", "циркон А4")
        assert materials_match("emo a3", "Циркон мультилеєр А3")
        assert not materials_match("pmma a4", "циркон А4")
        assert not materials_match("mono a4", "циркон А3")

    def test_what_to_do_is_not_what_from(self):
        assert materials_match("emo bl3", "фрезерування циркону бліч 3")

    def test_folder_that_says_nothing_is_a_candidate_for_anything(self):
        assert materials_match("mono a3", "attachments")
        assert materials_match("pmma a2", "Новая папка (12)")
        assert not materials_match("", "attachments")

    def test_different_lines_still_do_not_mix(self):
        assert not materials_match("pmma a3", "mono a3")
        assert not materials_match("mono a3", "pmma a3")
        assert not materials_match("emo a2", "Monolith A2")
        assert not materials_match("mono a2", "Emotions A2")
        assert not materials_match("tit", "mono a3")
