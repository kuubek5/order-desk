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
