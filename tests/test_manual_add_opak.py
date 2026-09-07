"""Опак у ручному додаванні.

Опак — покриття цирконієвої коронки рідиною, тарифікується окремо. У таблиці
його вписують у колонку «Коментар для CAM-оператора», і за зміну рахують очима
по цій колонці. Тому портал мусить писати РІВНО туди й у тій самій формі, що й
рука («2 opaq»): інакше рядок з порталу випаде з підрахунку.
"""

import pytest

from app.services.opak import format_opak, opak_units
from app.sheet_writer import COL_CAM_COMMENT, _row_value_map


@pytest.mark.parametrize(("entered", "expected"), [
    ("1", "1 opaq"),
    ("2", "2 opaq"),
    ("5", "5 opaq"),
    ("11", "11 opaq"),
    ("", ""),
    ("  ", ""),
    ("0", ""),                    # нуль опаків = коментаря немає
])
def test_opak_reads_like_a_handwritten_line(entered, expected):
    assert format_opak(entered) == expected

def test_free_text_survives_untouched():
    """Оператор іноді дописує уточнення; зʼїсти його гірше, ніж пропустити."""
    assert format_opak("2 opaq зверху") == "2 opaq зверху"


def test_the_comment_lands_in_the_cam_column():
    """Не в «вид роботи» й не в кінець рядка — саме в K, де його шукають."""
    cells = _row_value_map({
        "quantity": "2", "material_color": "мono a3", "e_value": "Басараб",
        "cam_comment": "3 opaq",
    })

    assert cells[COL_CAM_COMMENT] == "3 opaq"


def test_no_opak_leaves_the_comment_cell_alone():
    """Порожнє поле не має стирати коментар, який уже стоїть у рядку."""
    cells = _row_value_map({
        "quantity": "2", "material_color": "мono a3", "e_value": "Басараб",
        "cam_comment": "",
    })

    assert COL_CAM_COMMENT not in cells


@pytest.mark.parametrize(("comment", "expected"), [
    ("2 opaq", 2),
    ("1 opaq", 1),
    ("3 опака", 3),
    ("опак 2", 2),
    ("на швидку, 2 опака", 2),      # опак посеред живого коментаря
    ("ОПАК", 1),                    # слово без числа — один
    ("на швидку", None),            # про опак не сказано нічого
    ("", None),
    (None, None),
])
def test_reading_the_count_back_from_the_comment(comment, expected):
    """У таблиці опак пишуть роками по-різному; число має читатись з усіх
    звичних написань, інакше рядок мовчки випадає з підрахунку."""
    assert opak_units(comment) == expected


def test_nothing_said_and_zero_are_different_answers():
    """None = «у рядку про опак не сказано», 0 = «сказано, що опаку немає».
    Для звітності це різниця між «не заповнили» і «не було»."""
    assert opak_units("0 opaq") == 0
    assert opak_units("без опаку не потрібно") is not None   # слово згадане
    assert opak_units("мono a3") is None


def test_what_we_write_is_what_we_read():
    """Кільце має замикатись: що портал написав у таблицю, те він і рахує."""
    for n in (1, 2, 5, 11):
        assert opak_units(format_opak(str(n))) == n


def test_tooth_numbers_are_not_opak_counts():
    """Справжні коментарі з таблиці: «25 зуб покрити опаком». Номер зуба за
    FDI (11-48) стоїть поруч зі словом, а злиплі рядки ще й приліплюють його
    ЗЗАДУ. Нарахувати за це 25 опаків означає заплатити за роботу, якої
    не було."""
    assert opak_units("25 зуб покрити опаком") == 1
    assert opak_units("25 зуб покритиопаком25 зуб покрити опаком") == 1
    assert opak_units("опаком25 зуб") == 1


def test_a_plain_count_still_wins():
    """Затягуючи гайки проти номерів зубів, не можна втратити звичайну форму."""
    assert opak_units("1opaq") == 1
    assert opak_units("опак 3") == 3
    assert opak_units("12 opaq") == 12


def test_a_list_of_teeth_after_the_word_counts_as_its_length():
    """Живий коментар «покрити опаком 14,15» — це ДВА опаки на два зуби,
    а не чотирнадцять. Числа тут перелік, а не кількість."""
    assert opak_units("покрити опаком 14,15") == 2
    assert opak_units("опаком 14, 15, 16") == 3


def test_a_single_tooth_number_after_the_word_means_one():
    """«опаком 25» — 25-й зуб, один опак. Числа 11-18/21-28/31-38/41-48 за
    FDI — це зуби, і саме ними рясніють коментарі."""
    assert opak_units("покрити опаком 25") == 1
    assert opak_units("опаком 14") == 1
    # А от «опак 3» зубом бути не може — це кількість.
    assert opak_units("опак 3") == 3
