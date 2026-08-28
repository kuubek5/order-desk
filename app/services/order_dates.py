"""Business date of a work order.

Sheet-sourced work is dated by its Google tab name; email-sourced work has no
tab until it is accepted, so it falls back to the Kyiv business date of its
creation. Every screen that groups, filters or sorts by day goes through here,
so the queue, handout, archive and stats all agree on what "day" a work is on.
"""

import calendar
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import Order

try:
    BUSINESS_TIMEZONE = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:  # Windows Python may not bundle the IANA tz database.
    BUSINESS_TIMEZONE = None


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
            return created_utc.astimezone(BUSINESS_TIMEZONE).date()
        # Europe/Kyiv follows the EU transition rule. This fallback keeps the
        # app usable before `tzdata` is installed in a Windows development venv.
        year = created_utc.year
        march_last_sunday = 31 - (calendar.weekday(year, 3, 31) + 1) % 7
        october_last_sunday = 31 - (calendar.weekday(year, 10, 31) + 1) % 7
        dst_start = datetime(year, 3, march_last_sunday, 1, tzinfo=timezone.utc)
        dst_end = datetime(year, 10, october_last_sunday, 1, tzinfo=timezone.utc)
        offset = timedelta(hours=3 if dst_start <= created_utc < dst_end else 2)
        return (created_utc + offset).date()
    return date.today()


def sheet_order_key(order: Order) -> tuple:
    """(day, row position) — the same top-to-bottom order the lab reads off
    the physical table, used by the handout screen (see get_handout) as a
    rough readiness timeline instead of DB insertion order."""
    return (order_date(order), order.row_number if order.row_number is not None else 0)
