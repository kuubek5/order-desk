"""Робочий день цеху з межею НЕ опівночі (нічні зміни).

Бойовий випадок 03.09.26, 01:00: оператор нічної зміни опрацьовує 2 вересня, а
черга вже стрибнула на 3-тє. Ці тести стережуть, щоб межа лишалась о 07:30.
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pytest

from app.business_day import (
    DEFAULT_ROLLOVER,
    business_to_utc,
    business_today,
    get_rollover,
    parse_rollover,
    set_rollover,
    utc_to_business,
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
    # Список ЖОРСТКИЙ, і це його слабке місце: код, що переїхав у сервіс,
    # випадає зі сторожа мовчки. Саме так сталось 06.09.26, коли логіка черги
    # й видачі поїхала з роутерів у `app/services/` (аудит, крок 2.8) — дірку
    # видно лише якщо про неї памʼятати. Переносиш логіку екрана — додай файл
    # сюди тим самим комітом.
    screens = (
        "app/routers/queue.py",
        "app/routers/archive.py",
        "app/routers/orders.py",
        "app/routers/handout.py",
        "app/services/queue_view.py",
        "app/services/handout.py",
        "app/services/manual_add.py",
        "app/services/sheet_writeback.py",
        "app/services/mail_accept.py",
        # Тека файлів у `export` називається днем, і він мусить збігатися з
        # днем рядка в таблиці — інакше видача шукає роботу під одним днем,
        # а файли лежать під іншим (нічна зміна).
        "app/mail_export.py",
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


# Тести, яким КАЛЕНДАРНА дата справді потрібна — кожен із причиною. Решта
# тестів мусить рахувати дати робочою добою, інакше вони поводяться інакше
# між 00:00 і 07:30: падіння такого тесту видно лише вночі, і воно виглядає
# як «плаваючий» тест, а не як помилка (ревʼю 07.09.26, T.3).
CALENDAR_DATE_TESTS = {
    # Сам сторож — він і шукає рядок «date.today()» у чужому коді.
    "test_business_day.py": "сторож шукає цей рядок у тексті інших файлів",
    # Рядок CHANGELOG пишеться календарною датою релізу, не робочою добою.
    "test_bump_changelog.py": "звіряє дату в рядку CHANGELOG",
    # Фільтр IMAP будується календарною датою — так його розуміє сервер пошти.
    "test_mail_reader.py": "звіряє рядок SINCE у запиті до IMAP",
    # Тест нічної зміни навмисно говорить про обидві дати.
    "test_night_shift_day.py": "порівнює календарну добу з робочою — у цьому й суть",
    # Регресія 0.7.3: прогрів видачі лишався на date.today(), поки екран уже
    # жив робочою добою. Тест саме про цю розбіжність, тож обидві дати в ньому
    # обовʼязкові.
    "test_handout_routes.py": "перевіряє, що прогрів НЕ бере календарну дату",
}


def test_tests_themselves_use_the_business_day():
    """Той самий сторож, але для тестів.

    Тест, який будує дати через `date.today()`, між 00:00 і 07:30 працює з
    іншим днем, ніж застосунок. Такий тест або мовчки проходить удень і падає
    вночі, або — гірше — вночі проходить помилково. Обидва випадки виглядають
    як «плаваючий тест», і саме тому їх довго не чіпають.
    """
    tests_dir = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        if path.name in CALENDAR_DATE_TESTS:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            code = line.split("#", 1)[0]
            if "date.today()" in code:
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        "тести рахують календарну дату замість робочої доби: "
        + ", ".join(offenders)
        + ". Використайте business_today() або додайте файл у CALENDAR_DATE_TESTS "
        "з причиною."
    )


def test_the_calendar_date_allowlist_has_no_stale_entries():
    """Виняток, який більше нікому не потрібен, мовчки послаблює сторожа."""
    tests_dir = Path(__file__).resolve().parent
    stale = []
    for name in CALENDAR_DATE_TESTS:
        path = tests_dir / name
        if not path.is_file():
            stale.append(f"{name} (файлу немає)")
            continue
        if "date.today()" not in path.read_text(encoding="utf-8"):
            stale.append(f"{name} (більше не вживає date.today())")
    assert not stale, "застарілі винятки: " + ", ".join(stale)


def test_utc_from_the_db_is_shown_in_kyiv_time():
    """`SyncLog.occurred_at` лежить за Гринвічем (server_default на SQLite).
    08.09.26 о 20:17 підпис черги казав «Синхронізовано 17:15», і три години
    «мовчання» синку виглядали як збій. Показуємо київський час."""
    stored = datetime(2026, 9, 8, 17, 15)          # UTC, літній час (+3)
    assert utc_to_business(stored) == datetime(2026, 9, 8, 20, 15)
    assert business_to_utc(datetime(2026, 9, 8, 20, 15)) == stored
    # Зима (+2) — зсув не зашитий числом.
    assert utc_to_business(datetime(2026, 1, 10, 6, 0)) == datetime(2026, 1, 10, 8, 0)
    # Межа доби для фільтра журналу: «08.09 за Києвом» починається о 21:00 UTC 07.09.
    assert business_to_utc(datetime(2026, 9, 8)) == datetime(2026, 9, 7, 21, 0)
