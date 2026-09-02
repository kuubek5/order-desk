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


def parse_sheet_tab(sheet_tab: str | None) -> date | None:
    if not sheet_tab:
        return None
    try:
        return datetime.strptime(sheet_tab, "%d.%m.%y").date()
    except ValueError:
        return None


def order_date(order: Order) -> date:
    """Business date for both sheet and email sourced orders."""
    sheet_date = parse_sheet_tab(order.sheet_tab)
    if sheet_date is not None:
        return sheet_date
    if order.created_at is not None:
        created_utc = order.created_at.replace(tzinfo=timezone.utc)
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
