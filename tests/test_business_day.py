"""Робочий день цеху з межею НЕ опівночі (нічні зміни).

Бойовий випадок 03.09.26, 01:00: оператор нічної зміни опрацьовує 2 вересня, а
черга вже стрибнула на 3-тє. Ці тести стережуть, щоб межа лишалась о 07:30.
"""
from __future__ import annotations

from datetime import date, datetime, time

import pytest

from app.business_day import (
    DEFAULT_ROLLOVER,
    business_today,
    get_rollover,
    parse_rollover,
    set_rollover,
)


@pytest.fixture(autouse=True)
def _reset_rollover():
    set_rollover(None)  # типова 07:30
    yield
    set_rollover(None)


def test_default_is_half_past_seven():
    assert get_rollover() == time(7, 30)
    assert DEFAULT_ROLLOVER == time(7, 30)


def test_night_shift_after_midnight_still_on_previous_day():
    # РІВНО бойовий випадок: 01:00 третього — робочий день ще другого.
    assert business_today(datetime(2026, 9, 3, 1, 0)) == date(2026, 9, 2)


def test_just_before_rollover_is_previous_day():
    assert business_today(datetime(2026, 9, 3, 7, 29)) == date(2026, 9, 2)


def test_at_rollover_the_day_turns():
    assert business_today(datetime(2026, 9, 3, 7, 30)) == date(2026, 9, 3)


def test_daytime_is_the_calendar_day():
    assert business_today(datetime(2026, 9, 3, 14, 0)) == date(2026, 9, 3)
    assert business_today(datetime(2026, 9, 3, 23, 59)) == date(2026, 9, 3)


def test_evening_shift_start_is_already_the_new_day():
    # Нічний заступає ввечері 2-го — це ще 2-ге, і лишається ним до 07:30 3-го.
    assert business_today(datetime(2026, 9, 2, 20, 0)) == date(2026, 9, 2)
    assert business_today(datetime(2026, 9, 3, 4, 59)) == date(2026, 9, 2)


def test_rollover_is_configurable():
    set_rollover("06:00")
    assert business_today(datetime(2026, 9, 3, 5, 0)) == date(2026, 9, 2)
    assert business_today(datetime(2026, 9, 3, 6, 0)) == date(2026, 9, 3)


def test_bad_or_empty_setting_falls_back_to_default():
    for bad in ("", None, "   ", "не час", "25:99"):
        assert set_rollover(bad) == time(7, 30)


def test_parse_accepts_common_forms():
    assert parse_rollover("07:30") == time(7, 30)
    assert parse_rollover("7.30") == time(7, 30)
    assert parse_rollover("8") == time(8, 0)
    assert parse_rollover("хтозна") is None


def test_month_and_year_boundaries():
    set_rollover("07:30")
    assert business_today(datetime(2026, 9, 1, 2, 0)) == date(2026, 8, 31)
    assert business_today(datetime(2027, 1, 1, 3, 0)) == date(2026, 12, 31)


def test_retention_cutoff_is_the_same_day_source_everywhere():
    """Вікно ретеншену скрізь рахується від РОБОЧОЇ доби, а не календарної.

    Черга, архів і паспорт роботи мусять різати одну й ту саму межу: доки
    паспорт брав `date.today()`, а черга `business_today()`, вони щоночі
    розходились на добу — робота, яку черга показує живою, відкривалась
    замороженою «тільки для читання», і нічний оператор не міг вписати Sum3D.
    Правило «фільтр черги і _order_is_archived правити разом» (CLAUDE.md)
    тепер має сторожа, а не лише коментар.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    screens = (
        "app/routers/queue.py",
        "app/routers/archive.py",
        "app/routers/orders.py",
        "app/routers/handout.py",
    )
    offenders: list[str] = []
    for rel in screens:
        for number, line in enumerate(
            (root / rel).read_text(encoding="utf-8").splitlines(), start=1
        ):
            code = line.split("#", 1)[0]
            if "date.today()" in code:
                offenders.append(f"{rel}:{number}")
    assert not offenders, (
        "на цих екранах дата береться календарна, а не робоча "
        "(business_today): " + ", ".join(offenders)
    )
