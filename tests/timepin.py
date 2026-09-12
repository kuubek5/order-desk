"""Годинник на будній день — спільний для фікстури й імпортних констант.

Живе окремим модулем, а не в `conftest.py`, з однієї причини: частина тестів
рахує дату вкладки на РІВНІ МОДУЛЯ (`RECENT_DAY = ... - timedelta(days=3)`),
тобто в момент імпорту, коли жодна фікстура ще не працювала. Якщо константа
береться з реального годинника, а тіло тесту — з пінованого, у вихідні вони
розходяться на день: дві «різні» вкладки виявляються тією самою датою, і тест
падає на порожньому місці (спіймано 12.09.26).

Тому й фікстура `weekday_clock`, і такі константи беруть зсув ТУТ.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from app import business_day


def weekday_moment(now: Optional[datetime] = None) -> datetime:
    """Найближчий назад будній момент: пн-пт — як є, сб → пт, нд → пт.

    Час доби зберігається: межа робочого дня 07:30 має лишатись такою ж, як у
    справжньому прогоні.
    """
    now = now or business_day.business_now()
    return now - timedelta(days=max(0, now.weekday() - 4))


def pinned_today() -> date:
    """Робоча дата пінованого годинника — заміна `business_today()` у константах."""
    return business_day.business_date_of(weekday_moment())
