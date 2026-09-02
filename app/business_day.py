"""Робочий день цеху — межа НЕ опівночі.

Цех працює нічними змінами: нічний заступає ввечері, іде о 05:00, ранковий
приходить о 08:00. О 00:30 нічний оператор ще опрацьовує вчорашній день —
вкладку Google-таблиці за вчора, вчорашні роботи, вчорашню видачу. Календарна
північ для нього нічого не означає (бойовий випадок 03.09.26, 01:00: черга
стрибнула на новий день посеред зміни).

Тому «сьогодні» в застосунку — це БІЗНЕС-день: доба, що починається о
`rollover` (типово 07:30 за Києвом), а не о 00:00. Між північчю і 07:30
застосунок і далі показує вчорашню дату — саме те, над чим оператор працює.

Межа налаштовується (Налаштування → Шляхи й час), бо графік змін може
змінитись, а зашите число ми вже двічі проходили. Значення живе в памʼяті
процесу й оновлюється при збереженні налаштувань: `business_today()` кличеться
на КОЖЕН рядок черги (через `is_overdue`), тож ходити в БД тут не можна.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Часовий пояс цеху. Живе ТУТ (а не в order_dates), бо business_day — нижчий
# рівень: order_dates імпортує звідси, і зворотного напряму немає.
try:
    BUSINESS_TIMEZONE = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:  # Windows Python може не мати бази IANA.
    BUSINESS_TIMEZONE = None

# Типова межа: після 05:00 (нічний пішов) і до 08:00 (ранковий прийшов).
DEFAULT_ROLLOVER = time(7, 30)

_rollover: time = DEFAULT_ROLLOVER


def parse_rollover(value: str | None) -> time | None:
    """`"07:30"` → time(7, 30). Сміття → None (виклик сам вирішує, що робити)."""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H.%M", "%H"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def set_rollover(value: str | None) -> time:
    """Оновити межу для процесу. Порожньо/сміття → типова 07:30."""
    global _rollover
    _rollover = parse_rollover(value) or DEFAULT_ROLLOVER
    return _rollover


def get_rollover() -> time:
    return _rollover


def business_now() -> datetime:
    """Поточний момент за київським часом (де він у цеху й є)."""
    now = datetime.now(BUSINESS_TIMEZONE) if BUSINESS_TIMEZONE else datetime.now()
    return now


def business_date_of(moment: datetime) -> date:
    """Робоча дата ДЛЯ ВКАЗАНОГО моменту (час уже київський).

    Використовується і для «зараз», і для датування пошти: лист, прийнятий о
    00:30, належить робочому дню нічної зміни, а не наступному календарному —
    інакше він падав би у вкладку «Завтра» повз оператора.
    """
    if moment.time() < _rollover:
        return (moment - timedelta(days=1)).date()
    return moment.date()


def business_today(now: datetime | None = None) -> date:
    """Робоча дата ЗАРАЗ: до межі — вчорашня, після — сьогоднішня.

    Приклад із межею 07:30: о 01:00 третього вересня повертає 2 вересня (нічна
    зміна ще на вчорашньому дні), о 08:00 — вже 3 вересня.
    """
    return business_date_of(now or business_now())
