"""Business date of a work order.

Sheet-sourced work is dated by its Google tab name; email-sourced work has no
tab until it is accepted, so it falls back to the Kyiv business date of its
creation. Every screen that groups, filters or sorts by day goes through here,
so the queue, handout, archive and stats all agree on what "day" a work is on.
"""

import calendar
from datetime import date, datetime, timedelta, timezone

# BUSINESS_TIMEZONE живе в app.business_day (нижчий рівень); тут лише
# ре-експорт, щоб наявні імпорти `from app.services.order_dates import
# BUSINESS_TIMEZONE` не ламались.
from app.business_day import BUSINESS_TIMEZONE, business_date_of
from app.models import Order


# Назви вкладок дворозрядні («05.09.26»), а `%y` тлумачить 69-99 як 1969-1999:
# вкладка «01.01.70» розібралась би як 1970 рік і потягла б за собою роботу на
# півстоліття назад — повз будь-яке вікно черги, назавжди (аудит 05.09.26,
# синк LOW). Реальні вкладки лабораторії лежать біля сьогодні, тож дату поза
# розумним вікном вважаємо не датою взагалі: така вкладка просто не є
# «датованою», її не імпортують і не архівують.
_TAB_YEAR_MIN = 2000
_TAB_YEARS_AHEAD = 5


def parse_sheet_tab(sheet_tab: str | None) -> date | None:
    if not sheet_tab:
        return None
    try:
        parsed = datetime.strptime(sheet_tab, "%d.%m.%y").date()
    except ValueError:
        return None
    if parsed.year < _TAB_YEAR_MIN or parsed.year > date.today().year + _TAB_YEARS_AHEAD:
        return None
    return parsed


def order_date(order: Order) -> date:
    """Business date for both sheet and email sourced orders."""
    return order_date_of(order.sheet_tab, order.created_at)


def order_date_of(sheet_tab: str | None, created_at: datetime | None) -> date:
    """Те саме, але з двох полів, а не з обʼєкта роботи.

    Потрібно там, де дати рахують по ВСІХ живих роботах: тягнути заради двох
    колонок повні рядки ORM — зайва памʼять і час на кожен прохід
    (ревʼю 07.09.26, C.8)."""
    sheet_date = parse_sheet_tab(sheet_tab)
    if sheet_date is not None:
        return sheet_date
    if created_at is not None:
        created_utc = created_at.replace(tzinfo=timezone.utc)
        if BUSINESS_TIMEZONE is not None:
            # РОБОЧА дата, не календарна: лист, прийнятий о 00:30 нічною
            # зміною, належить її дню, інакше він падав би у «Завтра».
            return business_date_of(created_utc.astimezone(BUSINESS_TIMEZONE))
        # Europe/Kyiv follows the EU transition rule. This fallback keeps the
        # app usable before `tzdata` is installed in a Windows development venv.
        year = created_utc.year
        march_last_sunday = 31 - (calendar.weekday(year, 3, 31) + 1) % 7
        october_last_sunday = 31 - (calendar.weekday(year, 10, 31) + 1) % 7
        dst_start = datetime(year, 3, march_last_sunday, 1, tzinfo=timezone.utc)
        dst_end = datetime(year, 10, october_last_sunday, 1, tzinfo=timezone.utc)
        offset = timedelta(hours=3 if dst_start <= created_utc < dst_end else 2)
        return business_date_of((created_utc + offset).replace(tzinfo=None))
    from app.business_day import business_today

    return business_today()


def sheet_order_key(order: Order) -> tuple:
    """(day, row position) — the same top-to-bottom order the lab reads off
    the physical table, used by the handout screen (see get_handout) as a
    rough readiness timeline instead of DB insertion order."""
    return (order_date(order), order.row_number if order.row_number is not None else 0)
