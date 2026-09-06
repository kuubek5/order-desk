"""Нічна зміна: усе, що робить оператор о 02:00, належить УЧОРАШНЬОМУ дню.

Робочий день у лабораторії починається о 07:30 (`app/business_day.py`), тож між
північчю і ранком календарна дата вже нова, а зміна — та сама. Якщо частина
коду рахує «сьогодні» календарно, а частина — робочим днем, вони розходяться
рівно на ту зміну: рядок-нотатка лягає у вкладку одного дня, а файли — у теку
іншого, і на ранковій видачі оператор шукає роботу не там.

Ці тести фіксують саме стик двох місць, який 06.09.26 і розійшовся.
"""

from datetime import date, datetime

import pytest

from app.business_day import business_date_of, business_today
from app.mail_export import _batch_base_name, save_attachments_to_export


# День-«маячок» навмисно далеко від будь-якого «сьогодні»: якби код узяв
# календарну дату, збігтися з ним він не міг би НІКОЛИ. Перша версія цих тестів
# брала 06.09.26 — і мовчки проходила навіть із `date.today()`, бо саме той
# день і був сьогоднішнім. Тест, який неможливо провалити, нічого не стереже.
SENTINEL_DAY = date(2019, 3, 14)
SENTINEL_TAB = "14.03.19"


class TestNightShiftBelongsToYesterday:
    def test_business_day_rolls_at_half_past_seven(self):
        """Опорна перевірка: о 02:00 робочий день — ще вчорашній."""
        night = datetime(2026, 9, 7, 2, 0)
        assert business_date_of(night) == date(2026, 9, 6)
        morning = datetime(2026, 9, 7, 8, 0)
        assert business_date_of(morning) == date(2026, 9, 7)

    def test_export_folder_is_named_by_the_business_day(self, tmp_path, monkeypatch):
        """Тека партії в `export` — робочим днем, не календарним.

        Інакше о 02:00 файли лягли б у теку «07.09.26», тоді як робота в
        таблиці лишається на вкладці «06.09.26».
        """
        import app.mail_export as mail_export

        monkeypatch.setattr(mail_export, "business_today", lambda: SENTINEL_DAY)
        source = tmp_path / "crown.stl"
        source.write_bytes(b"STL")

        moved = save_attachments_to_export(
            tmp_path / "export", "Клієнт", "моно а3", [source]
        )

        assert moved[0].parent.parent.name == SENTINEL_TAB

    def test_batch_name_is_just_the_day(self):
        assert _batch_base_name(date(2026, 9, 6)) == "06.09.26"

    def test_accept_writes_to_the_business_day_tab(self, monkeypatch):
        """Вкладка для рядка-нотатки — теж робочий день.

        Разом із тестом вище це і є та сама пара, яка мусить збігатися.
        """
        import app.services.mail_accept as mail_accept

        seen = {}
        monkeypatch.setattr(mail_accept, "business_today", lambda: SENTINEL_DAY)
        monkeypatch.setattr(mail_accept, "open_spreadsheet", lambda db=None: object())
        def fake_lookup(ss, target):
            seen["target"] = target
            return None  # вкладки ще немає — лишається назва робочого дня

        monkeypatch.setattr(mail_accept, "latest_worksheet_on_or_before", fake_lookup)

        tab, _ = mail_accept._resolve_target_tab(db=None, email=type("E", (), {"id": 1})())

        assert seen["target"] == SENTINEL_DAY
        assert tab == SENTINEL_TAB


@pytest.mark.parametrize(
    "moment,expected",
    [
        (datetime(2026, 9, 7, 0, 5), date(2026, 9, 6)),   # одразу після півночі
        (datetime(2026, 9, 7, 7, 29), date(2026, 9, 6)),  # за хвилину до зміни
        (datetime(2026, 9, 7, 7, 30), date(2026, 9, 7)),  # рівно на межі
        (datetime(2026, 9, 7, 23, 59), date(2026, 9, 7)),
    ],
)
def test_business_day_boundary(moment, expected):
    assert business_date_of(moment) == expected


def test_business_today_is_a_real_date():
    assert isinstance(business_today(), date)
