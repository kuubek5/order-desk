"""Вкладка з пробілом у назві ламає ЗАПИС у таблицю, не читання.

Бойовий випадок 17.09.26: вкладку назвали « 17.09.26». Синк читав її нормально
(назву канонізує `canonical_tab_title`), а кнопка «Додати в чергу і таблицю»
віддавала оператору сирий
``APIError: [400]: Unable to parse range: ' 17.09.26'!B60:E260``.
Так само мовчки не доїжджали Sum3D, галочки й коментарі: діапазон A1 для БУДЬ-
ЯКОГО запису будується з назви вкладки. Тому назву лікуємо в самій таблиці —
перейменування йде по ID аркуша, а не по назві.
"""

import gspread
import pytest

from app import sheets


class FakeWorksheet:
    def __init__(self, title: str, sheet_id: int = 1):
        self.title = title
        self.id = sheet_id
        self.renamed_to: str | None = None
        self.rename_error: Exception | None = None

    def update_title(self, title: str) -> None:
        if self.rename_error is not None:
            raise self.rename_error
        self.renamed_to = title
        self.title = title


class FakeSpreadsheet:
    def __init__(self, *worksheets: FakeWorksheet):
        self._worksheets = list(worksheets)
        self.listings = 0

    def worksheet(self, name: str) -> FakeWorksheet:
        for ws in self._worksheets:
            if ws.title == name:
                return ws
        raise gspread.WorksheetNotFound(name)

    def worksheets(self) -> list[FakeWorksheet]:
        self.listings += 1
        return list(self._worksheets)


@pytest.fixture(autouse=True)
def _clear_worksheet_cache():
    """Кеш вкладок — thread-local і переживає тести, тож чистимо його явно."""
    sheets._local.worksheet_cache = {}
    yield
    sheets._local.worksheet_cache = {}


def test_a_padded_tab_title_is_repaired_in_the_sheet():
    padded = FakeWorksheet(" 17.09.26")
    ss = FakeSpreadsheet(padded)

    found = sheets.get_worksheet_by_name(ss, "17.09.26")

    assert found is padded, "вкладку треба знайти й зі зламаною назвою"
    assert padded.renamed_to == "17.09.26"
    assert padded.title == "17.09.26"


def test_a_clean_title_costs_no_extra_request():
    clean = FakeWorksheet("17.09.26")
    ss = FakeSpreadsheet(clean)

    assert sheets.get_worksheet_by_name(ss, "17.09.26") is clean
    assert clean.renamed_to is None
    assert ss.listings == 0, "чистити нічого — до Google звертатись не можна"


def test_a_tab_named_by_hand_is_left_alone():
    """Вкладку, яку людина свідомо назвала словами, не чіпаємо: пробіли в ній
    навмисні, а виправляти чужі назви — не наша справа."""
    notes = FakeWorksheet(" Нотатки  цеху")
    ss = FakeSpreadsheet(notes)

    assert sheets.get_worksheet_by_name(ss, "Нотатки цеху") is notes
    assert notes.renamed_to is None


def test_the_title_is_not_repaired_onto_an_existing_tab():
    """Чиста назва вже зайнята іншою вкладкою — Google відхилив би дублікат, а
    зливати два дні в один не можна. Вкладку віддаємо як є."""
    padded = FakeWorksheet(" 17.09.26", sheet_id=1)
    twin = FakeWorksheet("17.09.26", sheet_id=2)
    ss = FakeSpreadsheet(padded, twin)

    found = sheets.get_worksheet_by_name(ss, "17.09.26")

    assert found is twin, "точна назва має вигравати"
    assert padded.renamed_to is None


def test_a_failed_rename_does_not_break_the_lookup():
    """Акаунт лише «Читач» або гонка — перейменування впало. Читання ж
    працює, тож виклик має повернути вкладку, а не впасти."""
    padded = FakeWorksheet(" 17.09.26")
    # Будь-яка помилка, не лише APIError: гілка ловить широко саме тому, що
    # зламати перейменування може й мережа, і права, і гонка.
    padded.rename_error = PermissionError("The caller does not have permission")
    ss = FakeSpreadsheet(padded)

    assert sheets.get_worksheet_by_name(ss, "17.09.26") is padded
    assert padded.title == " 17.09.26"
