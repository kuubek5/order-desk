"""Queue screen domain logic: ordering, the day-strip window, retention and
the dashboard peek-card summaries.

No Request, no Response — everything here takes plain orders/dates and returns
plain values, so the same rules are testable (and reusable) without HTTP.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, ReworkRecord, SyncLog
from app.services.formatting import pluralize_uk
from app.services.order_dates import order_date, parse_sheet_tab
from app.stats import parse_int_safe
from app.statuses import is_overdue

DATE_STRIP_WINDOW = 3

# Working-space retention: the queue, day-strip, KPIs and handout only surface
# orders whose business date is within this many days back. Older work rolls
# into the Archive screen (kept in the DB, never deleted) so the daily workspace
# stays "the last month" while history stays findable. Google-tab deletions of
# older days therefore never remove anything the operator still works with.
RETENTION_DAYS = 30

# Column headers the operator can click to sort the queue table (queue.html
# thead, via _sortable_th.html). Explicit, opt-in — with no `sort` query
# param the queue keeps its default urgency-based queue_sort_key ordering.
QUEUE_SORT_FIELDS = ("material", "kind", "quantity")


# Слова, якими технік у коментарі просить швидку програму спікання.
# Це ЄДИНИЙ писаний сигнал терміновості для лабораторної роботи (CLAUDE.md §2:
# «техніки пишуть у коментарі "на швидку"… трапляється регулярно»), і до цього
# він ніде не був реалізований: рядок підсвічувався за наявністю "!!", тобто
# «Чекаємо Скани!!!!» (блокер, не терміновість) читалось як термінове, а
# «Якщо можно на швидку, я сам закрию» — ні. Сигнал був інвертований.
RUSH_MARKERS = ("на швидку", "на швидке", "швидку", "швидке", "терміново")


def is_rush_comment(text: str | None) -> bool:
    """Чи просить коментар техніка швидку програму (= термінова робота)."""
    if not text:
        return False
    lowered = text.casefold()
    return any(marker in lowered for marker in RUSH_MARKERS)


def queue_sort_key(order: Order) -> tuple:
    """Oldest overdue work first, then the earliest daily deadline."""
    due_rank = {"09:00": 0, "14:00": 1, "16:00": 2}.get(order.due_time, 3)
    overdue_rank = 0 if is_overdue(order.sheet_tab, order.status) else 1
    return overdue_rank, order_date(order), due_rank, order.id


def queue_column_sort_value(order: Order, sort: str) -> int | str | None:
    """Sort value for one column, or None for "blank" (missing/unparseable)
    — callers must always sort blanks last, never first, regardless of
    direction (an operator sorting "by material" doesn't want blanks at the
    top just because they picked descending)."""
    if sort == "quantity":
        # Order.quantity is a free-text Mapped[Optional[str]] column (see
        # app/models.py), not a number — reuse the same defensive parser
        # app/stats.py already established for this exact field instead of
        # writing a third copy of "parse this string as an int or give up".
        return parse_int_safe(order.quantity)
    field = "material_color" if sort == "material" else "kind"
    value = getattr(order, field, None)
    if value is None or not value.strip():
        return None
    return value.strip().lower()


def sort_orders_by_column(orders: list[Order], sort: str, direction: str) -> list[Order]:
    """Stable sort by one queue column. Blank/unparseable values always sort
    last, in both directions — only the *present* values reverse order."""
    reverse = direction == "desc"
    paired = [(order, queue_column_sort_value(order, sort)) for order in orders]
    present = sorted((p for p in paired if p[1] is not None), key=lambda p: p[1], reverse=reverse)
    missing = [order for order, value in paired if value is None]
    return [order for order, _ in present] + missing


def order_is_archived(order: Order, cutoff: date) -> bool:
    """The Archive holds everything NOT in the working queue: orders explicitly
    archived (removed from Google — a tab or a row) OR aged past the retention
    window. The exact complement of the working-set filter in get_queue."""
    return order.archived_at is not None or order_date(order) < cutoff


def known_order_dates(db: Session) -> list[date]:
    """Calendar days that actually have order data, derived straight from
    `Order.sheet_tab` — the same column `app/sync.py` populates verbatim
    from real Google Sheet tab names, and that `accept_email` stamps with
    the Kyiv business date for mail-sourced orders. There is deliberately no
    separate mechanism here that talks to Google Sheets to list its tabs:
    `sheet_tab` already mirrors that list, refreshed every background sync
    tick, so the queue's day-strip stays in sync "for free"."""
    tabs = db.scalars(select(Order.sheet_tab).where(Order.sheet_tab.isnot(None)).distinct()).all()
    parsed = {d for d in (parse_sheet_tab(tab) for tab in tabs) if d is not None}
    return sorted(parsed)


def date_window(
    known_dates: list[date], today: date, date_page: int | None, window: int = DATE_STRIP_WINDOW
) -> tuple[list[date], int, int]:
    """Page through `known_dates` (ascending) `window` days at a time.

    Returns `(visible_dates, current_page, total_pages)`. Pages tile from the
    RIGHT (most-recent) end: page 0 is the newest full window ending at the last
    date, higher page numbers step further back in time (the oldest page may be
    partial). This keeps the default view a full recent week rather than a stub
    when `today` happens to fall one short of a left-aligned block boundary.

    With no explicit `date_page`, the default is the page whose window contains
    `today` (or the newest page if today has no data yet). `date_page` counts
    back from the newest window, so the template's ‹ (older) is `date_page + 1`
    and › (newer) is `date_page - 1`."""
    if not known_dates:
        return [], 0, 0

    n = len(known_dates)
    total_pages = (n + window - 1) // window

    if date_page is None:
        anchor_idx = known_dates.index(today) if today in known_dates else n - 1
        # How many whole windows the anchor sits back from the newest date.
        current_page = ((n - 1) - anchor_idx) // window
    else:
        current_page = max(0, min(date_page, total_pages - 1))

    end = n - current_page * window
    start = max(0, end - window)
    return known_dates[start:end], current_page, total_pages


def handout_pending_client_count(orders: list[Order], today: date) -> int:
    """Distinct clients with an outstanding (pre-today, not yet issued) order —
    the same candidate rule get_handout groups by, minus the filesystem scan,
    so the queue dashboard's KPI/peek cards stay cheap on every page load."""
    clients: set[str] = set()
    for order in orders:
        if not order.client_name or order.status == "видано":
            continue
        day = parse_sheet_tab(order.sheet_tab)
        if day is not None and day >= today:
            continue
        clients.add(order.client_name)
    return len(clients)


def queue_handout_summary(orders: list[Order], today: date) -> str:
    count = handout_pending_client_count(orders, today)
    if count == 0:
        return "Усе видано"
    noun = pluralize_uk(count, "клієнт очікує", "клієнти очікують", "клієнтів очікують")
    return f"{count} {noun}"


def queue_week_summary(db: Session, all_orders: list[Order], today: date) -> str:
    """Compact "quantity milled · rework %" line for the Статистика peek card.

    Reuses the order list get_queue already fetched instead of re-running
    get_stats's full scan, plus one light ReworkRecord query scoped to the
    same window — a summary card, not a duplicate of the stats screen.
    """
    week_start = today - timedelta(days=6)
    week_orders = [o for o in all_orders if week_start <= order_date(o) <= today]
    quantities = (parse_int_safe(o.quantity) for o in week_orders)
    quantity_sum = sum(q for q in quantities if q is not None)

    week_records = db.scalars(
        select(ReworkRecord).where(
            ReworkRecord.created_at >= datetime.combine(week_start, datetime.min.time())
        )
    ).all()
    redo_quantities = (parse_int_safe(r.redo_quantity) for r in week_records)
    redo_sum = sum(q for q in redo_quantities if q is not None)

    if quantity_sum == 0:
        return "Ще немає даних за тиждень"
    if redo_sum == 0:
        return f"{quantity_sum} од. · без браку"
    rework_pct = round(redo_sum / quantity_sum * 100)
    return f"{quantity_sum} од. · брак {rework_pct}%"


def queue_sync_summary(db: Session) -> str:
    """Last Google Sheets import outcome for the queue dashboard's peek card."""
    last_sync = db.scalar(
        select(SyncLog)
        .where(SyncLog.direction == "sheet_to_db")
        .order_by(SyncLog.occurred_at.desc())
        .limit(1)
    )
    if last_sync is None:
        return "Ще не синхронізовано"
    time_label = last_sync.occurred_at.strftime("%H:%M") if last_sync.occurred_at else "—"
    if last_sync.status == "error":
        return f"Помилка синхронізації ({time_label})"
    return f"Синхронізовано {time_label}"
