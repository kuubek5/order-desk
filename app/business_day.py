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
from datetime import date, datetime, time, timedelta, timezone
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


# ── Вкладка дня: вихідні пишуть у п'ятницю ──────────────────────────────────
# Цех працює в суботу й неділю, а офіс, техніки й логісти — ні, і вкладок за
# сб/нд у таблиці НЕМАЄ: роботи вихідних і прийняті тоді листи пишуть у
# вкладку п'ятниці (власник 11.09.26). Тож «сьогоднішня вкладка» в суботу —
# п'ятнична, а «вчора» в понеділок — теж п'ятниця, не порожня неділя. Робоча
# ДАТА (`business_today`) при цьому лишається справжньою: вона про годинник,
# а ці функції — про те, у яку вкладку лягає день.


def tab_day(day: date) -> date:
    """Вкладка, у яку пишуть цей день: субота й неділя → п'ятниця."""
    if day.weekday() >= 5:
        return day - timedelta(days=day.weekday() - 4)
    return day


def prev_tab_day(day: date) -> date:
    """Попередня вкладка перед вкладкою `day` (у понеділок — п'ятниця)."""
    previous = tab_day(day) - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def next_tab_day(day: date) -> date:
    """Наступна вкладка після вкладки `day` (у п'ятницю — понеділок)."""
    following = tab_day(day) + timedelta(days=1)
    while following.weekday() >= 5:
        following += timedelta(days=1)
    return following


def business_tab_today(now: datetime | None = None) -> date:
    """Вкладка, у яку пишуть ЗАРАЗ (у вихідні — п'ятнична)."""
    return tab_day(business_today(now))


def utc_now() -> datetime:
    """UTC-час без часового поясу — заміна `datetime.utcnow()`.

    Значення те саме, що й раніше (усі колонки `DateTime` в базі наївні, і
    міняти це зараз означало б переписати всі порівняння). Різниця лише в тому,
    що `utcnow()` оголошено застарілим у Python 3.12: коли інтерпретатор на
    робочому ПК оновиться, воно почне сипати попередженнями, а колись і зникне.
    Одна функція замість п'ятнадцяти викликів — і міняти буде що одне місце.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_to_business(moment: datetime) -> datetime:
    """Наївний UTC з бази (`server_default=func.now()` на SQLite) → наївний
    київський час для ПОКАЗУ.

    Журнал синку й підпис «Синхронізовано HH:MM» показували час за Гринвічем:
    о 20:17 у цеху віджет казав «17:15», і три години «мовчання» синку
    виглядали як збій, якого не було (08.09.26). Порівняння в базі лишаються в
    UTC — це лише для очей.
    """
    if BUSINESS_TIMEZONE is None:
        # Без бази IANA — хоч місцевий час машини: краще за Гринвіч у цеху.
        return moment.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return (
        moment.replace(tzinfo=timezone.utc)
        .astimezone(BUSINESS_TIMEZONE)
        .replace(tzinfo=None)
    )


def canonical_tab_title(title: str | None) -> str:
    """Назва вкладки таблиці без «сміття» навколо: пробіли, нерозривні
    пробіли, невидимі символи зі скопійованої назви.

    08.09.26 вкладку в Google Таблиці назвали « 08.09.26» (пробіл на початку).
    Шаблон `дд.мм.рр` її не впізнав, синк вважав вкладку недатованою й ніколи
    не читав — робочий день стояв «Сьогодні 0» без жодного запису в журналі.
    Усе, що порівнює назву вкладки з датою, іде через цю функцію.
    """
    text = title or ""
    # NBSP, вузький NBSP, zero-width space, BOM, word joiner.
    for junk in ("\u00a0", "\u202f", "\u200b", "\ufeff", "\u2060"):
        text = text.replace(junk, " ")
    return " ".join(text.split())


def business_to_utc(moment: datetime) -> datetime:
    """Зворотне до `utc_to_business`: межа дня з фільтра журналу → UTC для WHERE."""
    if BUSINESS_TIMEZONE is None:
        return moment.astimezone(timezone.utc).replace(tzinfo=None)
    return (
        moment.replace(tzinfo=BUSINESS_TIMEZONE)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
