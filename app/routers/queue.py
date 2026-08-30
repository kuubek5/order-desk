"""Черга робіт — основний екран, за яким оператор проводить 90% часу.

Показує всі незавершені роботи обох джерел (лабораторія і пошта) незалежно
від дня, з фільтрами-чіпами, смужкою днів і бічною панеллю стану синку.

Тут же ручні дії над самою синхронізацією: пауза, разовий синк і одноразовий
імпорт усієї історії таблиці.
"""

import logging
import time
from datetime import date, datetime, timedelta
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import nullslast, select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from app import sync_control
from app.models import EmailMessage, Order
from app.order_folder import (
    attach_email_folder_availability,
    attach_email_preview_tokens,
    attach_export_folder_uris,
    attach_job_code_folder_uris,
)
from app.queue_filters import (
    READY_FILTERS,
    SOURCE_FILTERS,
    count_by_readiness,
    count_by_source,
    filter_by_readiness,
    filter_by_source,
)
from app.routers.deps import get_current_user, get_db, templates
from app.services.config_state import (
    mail_preview_roots,
    mail_trusted_roots,
    sheets_configured,
)
from app.services.order_dates import order_date, parse_sheet_tab
from app.services.focus import count as focus_count, focused_ids, ranks as focus_ranks
from app.services.furnace import strip_cards as furnace_cards, strip_summary as furnace_summary
from app.services.shift import open_notes as open_shift_notes
from app.services.queue import (
    DATE_STRIP_WINDOW,
    QUEUE_SORT_FIELDS,
    RETENTION_DAYS,
    date_window,
    handout_pending_client_count,
    known_order_dates,
    order_is_archived,
    queue_handout_summary,
    queue_sort_key,
    queue_sync_summary,
    queue_week_summary,
    sort_orders_by_column,
)
from app.sheet_sync_service import SheetSyncError, summary_message, sync_google_sheets
from app.statuses import STATUSES, is_overdue
from app.sync_control import (
    SYNC_SPEED_PRESETS,
    get_speed_preset,
    get_sync_speed,
    record_viewed_day,
    set_speed_preset,
)
from app.sync_heartbeat import sync_status_pair
from app.triage_status import triage_readiness

logger = logging.getLogger(__name__)

router = APIRouter()


def sum_units(orders) -> int:
    """Total units across the given orders. Sums only cleanly-integer quantity
    strings — the sheet's quantity column is free text, so ranges ("13-23") or
    blanks are skipped rather than guessed at, keeping the count honest."""
    total = 0
    for order in orders:
        value = (order.quantity or "").strip()
        if value.isdigit():
            total += int(value)
    return total


@router.get("/", response_class=HTMLResponse)
def get_queue(
    request: Request,
    period: str = "today",
    ready: str = "all",
    source: str = "all",
    overdue: str = "0",
    # «Мої зараз»: mine=1 лишає в таблиці лише мій робочий набір. Назва саме
    # `mine`, бо `focus` у цьому роуті ВЖЕ зайнятий — ним «Останні дії»
    # передають id рядка, на який треба проскролити. Значення мусить
    # потрапити і в rows_qs, інакше перший же 15-секундний тік полла
    # поверне приховані рядки назад.
    mine: str = "",
    # `date` (query key) can't be the python parameter name — it would
    # shadow the `date` class imported at module level and used throughout
    # this function (`date.today()` etc). `Annotated` keeps the *default*
    # value a plain `""`/`None` (not a `Query(...)` sentinel object), so
    # calling `get_queue(...)` directly in tests — the established pattern
    # in this file, see tests/test_mail_queue_backend.py — still works
    # without going through FastAPI's request-parsing layer.
    date_param: Annotated[str, Query(alias="date")] = "",
    date_page: int | None = None,
    sort: str = "",
    # `dir` (query key) is kept off the python parameter name so it doesn't
    # shadow the `dir()` builtin anywhere in this function's body — same
    # spirit as the `date`/`date_param` split above.
    sort_dir: Annotated[str, Query(alias="dir")] = "asc",
    # `partial=rows` returns only the queue-rows fragment (for the 15s HTMX
    # poll that keeps the table in step with the sheet without a full reload).
    partial: str = "",
    # Order id the page should scroll to and highlight once loaded — set by the
    # «Останні дії» popup when the action it points at lives on another day tab
    # or behind different filters, so the jump survives the navigation.
    focus: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    # Validate period parameter
    if period not in ("today", "yesterday", "tomorrow", "earlier"):
        period = "today"

    # Validate ready parameter (second, independent filter — CLAUDE.md section 9, screen 1)
    if ready not in READY_FILTERS:
        ready = "all"

    # Validate source independently from the period/readiness filters.
    if source not in SOURCE_FILTERS:
        source = "all"

    # Validate the optional column sort (queue.html thead, via
    # _sortable_th.html). Absent/invalid `sort` means "no explicit column
    # sort" — the queue keeps its default urgency-based ordering below.
    if sort not in QUEUE_SORT_FIELDS:
        sort = ""
    if sort_dir not in ("asc", "desc"):
        sort_dir = "asc"

    # "Прострочено" KPI shortcut: overdue work can land in either the
    # "yesterday" or "earlier" bucket, so it needs its own cross-period view
    # rather than a period value. Independent of, and takes priority over,
    # the period tabs — clicking any period/source/ready filter link drops it
    # (those links never carry `overdue`).
    show_overdue = overdue == "1"

    # Day-strip filter (sidebar "Дні" group): an explicit, single calendar
    # day chosen from the set of days that actually have order data (see
    # `known_order_dates` — sourced from `Order.sheet_tab`, so it's always
    # in sync with whatever tabs the Sheet has, no separate lookup needed).
    # Same precedence rule as `show_overdue` above: independent of, and
    # takes priority over, the period bucket for this request; `source`/
    # `ready` stay independent and still apply on top either way. An
    # invalid/unparseable value is silently ignored (falls back to `period`)
    # rather than erroring, same spirit as the period/ready/source fallbacks.
    selected_date = parse_sheet_tab(date_param)

    # Fetch all orders (eager-load material for the queue's material badge)
    all_orders = db.scalars(
        select(Order).options(selectinload(Order.material)).order_by(Order.id.desc())
    ).all()

    # Define date boundaries
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    # Working space = active orders within the retention window. Archived orders
    # (removed from Google or explicitly archived) and orders older than
    # RETENTION_DAYS drop out of every working view here — they stay in the DB
    # and are reachable on the Archive screen. Done in Python (not SQL) because
    # the business date is derived from sheet_tab, not a stored column, and the
    # order set is small (tens per day, a few thousand total).
    retention_cutoff = today - timedelta(days=RETENTION_DAYS)
    all_orders = [
        o
        for o in all_orders
        if o.archived_at is None and order_date(o) >= retention_cutoff
    ]

    # Categorize orders into buckets
    buckets = {"today": [], "yesterday": [], "tomorrow": [], "earlier": []}

    for order in all_orders:
        day = order_date(order)
        if day == today:
            buckets["today"].append(order)
        elif day == yesterday:
            buckets["yesterday"].append(order)
        elif day == tomorrow:
            buckets["tomorrow"].append(order)
        else:
            buckets["earlier"].append(order)

    # Get the filtered list for the current period, every overdue order
    # across all periods when the "Прострочено" KPI shortcut is active, or
    # exactly one calendar day when a day-strip date is selected. `overdue`
    # keeps top priority (unchanged, pre-existing behavior); `date` is the
    # next priority, ahead of the plain period bucket.
    if show_overdue:
        orders = sorted(
            (o for o in all_orders if is_overdue(o.sheet_tab, o.status)),
            key=queue_sort_key,
        )
    elif selected_date is not None:
        orders = sorted(
            (o for o in all_orders if order_date(o) == selected_date),
            key=queue_sort_key,
        )
    else:
        orders = sorted(buckets[period], key=queue_sort_key)

    # Робочий набір оператора — одним запитом, до будь-яких циклів по рядках.
    # Не просто множина, а ще й порядок пришпилення: за ним нижче шикуються
    # пришпилені рядки, і рівно той самий порядок тримає клієнт.
    my_focus_rank = focus_ranks(db, user)
    my_focus = set(my_focus_rank)
    focus_mine = mine == "1"
    if focus_mine:
        orders = [o for o in orders if o.id in my_focus]

    # Source chip counts cover the selected period before applying source.
    source_counts = count_by_source(orders)
    orders = filter_by_source(orders, source)

    # Count for all buckets
    counts = {k: len(v) for k, v in buckets.items()}

    attach_export_folder_uris(db, orders)
    attach_job_code_folder_uris(db, orders)

    # Second, independent filter: readiness (has the technician dropped files yet?)
    ready_counts = count_by_readiness(orders)
    orders = filter_by_readiness(orders, ready)

    # Explicit, opt-in column sort (queue.html thead) applied last, on top
    # of whatever period/source/date/ready filtering produced above. With no
    # `sort`, this is a no-op — the default urgency-based queue_sort_key
    # ordering from earlier is left completely untouched.
    if sort:
        orders = sort_orders_by_column(orders, sort, sort_dir)

    # Queue table visually separates lab-sheet rows from mail-sourced rows
    # (queue.html: "Лабораторні роботи" / "Роботи з пошти") — mirrors both
    # the real Google Sheet's own convention (lab rows in the main block,
    # mail placeholder rows appended below, see append_mail_placeholder_row)
    # and gives each source its own collapsible section. Splitting the
    # already-filtered-and-sorted `orders` list preserves every filter/sort
    # applied above; each sublist stays correctly ordered within itself.
    # Mirror the sheet's own hierarchy in the neutral, unfiltered view: internal
    # lab works (the main table region) above the наряд-less client/mail rows
    # (the region below it) — the queue table renders this flat `orders` list, so
    # the ordering has to happen here. Only when the operator hasn't narrowed or
    # re-sorted anything (source=all, ready=all, no explicit column sort, no
    # overdue shortcut), so a deliberate sort/filter still wins. Stable: the
    # urgency order within each group is preserved, лаб rows just float on top.
    if source == "all" and ready == "all" and not sort and not show_overdue:
        orders.sort(key=lambda o: 0 if o.source == "lab" else 1)

    # Пришпилені — нагору. Останнім кроком, тобто поверх будь-якого фільтра чи
    # ручного сортування: набір «мої зараз» це те, що оператор ТРИМАЄ В РУКАХ,
    # і шукати його щоразу серед шестисот рядків — та сама робота, від якої
    # шпилька мала звільнити. Сортування стабільне, тому всередині кожної
    # групи порядок терміновості лишається незмінним, а поділ «лабораторія /
    # пошта» нижче переживає це без змін.
    #
    # Правило стабільного порядку (CLAUDE.md §2) не порушується: воно про те,
    # що список не має рухатись САМ — фоновий полл цього порядку не міняє, бо
    # рахує його з тих самих даних. Рядок переїжджає лише у відповідь на
    # свідоме клацання по шпильці.
    if my_focus:
        # Ключ — місце в наборі за часом пришпилення; непришпилені йдуть після
        # всіх. Стабільне сортування зберігає порядок терміновості серед
        # непришпилених, а серед пришпилених порядок задає сам набір.
        #
        # Це головне, що тримає рядки на місці: нова мітка стає В КІНЕЦЬ
        # набору, тож жоден уже пришпилений рядок не рухається. Коли сервер
        # шикував пришпилені за порядком черги, а клієнт клав щойно
        # пришпилену вгору, кожна нова шпилька перемішувала весь набір.
        big = len(my_focus_rank)
        orders.sort(key=lambda o: my_focus_rank.get(o.id, big))

    orders_lab = [o for o in orders if o.source != "email"]
    orders_email = [o for o in orders if o.source == "email"]

    # Pop flashes only on a full-page render — the 15s poll (partial="rows")
    # would otherwise consume them before the real navigation shows them.
    sync_flash = request.session.pop("sync_flash", None) if partial != "rows" else None
    toast_flash = request.session.pop("toast_flash", None) if partial != "rows" else None
    # Newest-first, matching the /mail triage list exactly — the pinned widget
    # is a peek of the SAME queue, so the two must agree on order (an opposite
    # sort made the widget's top rows look like different letters).
    pending_emails = db.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.status == "нове",
            # Rule-filtered letters (3D print, accounting, spam) live on the
            # triage screen's «Відфільтровані» tab — keep the queue widget to
            # actual milling work.
            EmailMessage.filter_category.is_(None),
        )
        .options(selectinload(EmailMessage.attachments))
        .order_by(
            EmailMessage.received_at.desc().nullslast(),
            EmailMessage.created_at.desc(),
            EmailMessage.id.desc(),
        )
    ).all()
    attach_email_folder_availability(
        pending_emails,
        mail_trusted_roots(db),
    )
    attach_email_preview_tokens(pending_emails, mail_trusted_roots(db), mail_preview_roots(db))
    pending_mail_count = len(pending_emails)

    # Dashboard header (Варіант B): KPI row (small, hard counts) and peek row
    # (state of the three neighboring screens) — every card is a real link/
    # filter, computed from data already fetched above plus at most one light
    # extra query each, never the heavy export-folder scan or a duplicate of
    # get_stats' full pass.
    overdue_count = sum(1 for o in all_orders if is_overdue(o.sheet_tab, o.status))
    due_today_count = sum(1 for o in buckets["today"] if o.status != "видано")
    clients_without_handout = handout_pending_client_count(all_orders, today)

    kpis = {
        "overdue": overdue_count,
        "due_today": due_today_count,
        "pending_mail": pending_mail_count,
        "clients_without_handout": clients_without_handout,
    }
    peeks = {
        "handout": queue_handout_summary(all_orders, today),
        "stats": queue_week_summary(db, all_orders, today),
        "sync": queue_sync_summary(db),
    }
    sync_status = sync_status_pair(db, datetime.now())

    # Day-strip: 7 known dates at a time out of every distinct day that has
    # order data (see `known_order_dates` / `date_window` docstrings above
    # for why this is enough to stay in sync with the Sheet with no new
    # sync mechanism).
    # Day-strip days come from the WORKING set (already filtered to active +
    # within the retention window above), so the strip shows only days the
    # operator still works with — never archived/older days and never a phantom
    # "today" without a real tab. date_window uses `today` only to pick the
    # default page (lands on the newest real day when today isn't among them).
    date_universe = sorted({order_date(o) for o in all_orders})
    date_tabs, current_date_page, total_date_pages = date_window(date_universe, today, date_page)

    # Query string of the currently active filters, so the 15s poll fragment
    # re-requests the exact same view it lives in. Built from the validated
    # params (not request.query_params) so it also works when get_queue is
    # called directly in tests, and reflects clamped/validated values.
    _qs_items: list[tuple[str, str]] = []
    if show_overdue:
        _qs_items.append(("overdue", "1"))
    _qs_items += [("period", period), ("ready", ready), ("source", source)]
    if focus_mine:
        _qs_items.append(("mine", "1"))
    if date_param:
        _qs_items.append(("date", date_param))
        _qs_items.append(("date_page", str(current_date_page)))
    if sort:
        _qs_items += [("sort", sort), ("dir", sort_dir)]
    rows_qs = urlencode(_qs_items)

    # The single day this operator is actually looking at. Two consumers:
    # the hot sync lane (so the open tab is always among the fast-synced ones)
    # and the manual-add form, which writes its row into THIS tab.
    # It must not be `selected_date` alone: picking a day from the date strip
    # sets it, but the «Завтра»/«Вчора» period tabs do not put a date in the
    # URL at all — an add made from those then silently fell back to today.
    if selected_date is not None:
        viewed_day = selected_date
    elif period == "yesterday":
        viewed_day = today - timedelta(days=1)
    elif period == "tomorrow":
        viewed_day = today + timedelta(days=1)
    elif period == "today" and not show_overdue:
        viewed_day = today
    else:
        viewed_day = None  # "earlier"/overdue span many days — no single tab

    context = {
            "page_title": "Черга робіт",
            "orders": orders,
            "orders_lab": orders_lab,
            "orders_email": orders_email,
            # Sum of units across the currently-filtered view (period/source/
            # ready/date/overdue all already applied to `orders`). Only cleanly
            # numeric quantities count; ranges/blanks are skipped rather than
            # guessed. Shown next to the "N у вигляді" live counter.
            "total_units": sum_units(orders),
            "user": user,
            "statuses": STATUSES,
            "period": period,
            "counts": counts,
            "ready": ready,
            "ready_counts": ready_counts,
            "source": source,
            "source_counts": source_counts,
            "show_overdue": show_overdue,
            "kpis": kpis,
            "peeks": peeks,
            "sync_status": sync_status,
            "has_any_orders": bool(all_orders),
            "sheets_configured": sheets_configured(db),
            "sync_flash": sync_flash,
            "toast_flash": toast_flash,
            "pending_emails": pending_emails,
            "pending_mail_count": pending_mail_count,
            # Записки передачі зміни для картки над чергою
            # (app/templates/_shift_card.html). Далі картка оновлює себе
            # сама через /shift/card — тут лише перший рендер.
            "shift_open_notes": open_shift_notes(db),
            # Печі для смуги над чергою (app/templates/_furnace_strip.html).
            # Далі смуга оновлює себе сама через /furnaces/strip — тут лише
            # перший рендер, щоб на завантаженні сторінки не було 30 секунд
            # порожнечі. Читає стан у памʼяті, до печей не ходить.
            "furnace_cards": (_furnace_cards := furnace_cards(db)),
            "furnace_summary": furnace_summary(_furnace_cards),
            # Мітки «мої зараз» — персональні, тому контекст, а не глобал.
            # ОБИДВІ гілки (сторінка й partial=rows) читають цей самий
            # словник: якби полл рахував інакше, мітки зникали б кожні 15с.
            "focused_ids": my_focus,
            "focus_count": focus_count(db, user),
            "focus_mine": focus_mine,
            "selected_date": selected_date,
            "date_tabs": date_tabs,
            "date_page": current_date_page,
            "total_date_pages": total_date_pages,
            "sort": sort,
            "sort_dir": sort_dir,
            "rows_qs": rows_qs,
            "sync_speed": SYNC_SPEED_PRESETS,
            "sync_speed_active": sync_control.get_speed_preset(),
            "sync_screen_seconds": get_sync_speed()["screen"],
            "viewed_tab": viewed_day.strftime("%d.%m.%y") if viewed_day else "",
            "sync_paused": sync_control.is_paused(),
            "focus_order_id": focus.strip() if focus.strip().isdigit() else "",
    }

    record_viewed_day(viewed_day)

    # The screen poll asks for just the rows block; everything else (sidebar
    # counts, KPIs) refreshes on a full navigation or a manual sync.
    if partial == "rows":
        return templates.TemplateResponse(request, "_queue_rows.html", context)

    return templates.TemplateResponse(request, "queue.html", context)


@router.post("/sync-speed", response_class=HTMLResponse)
def set_sync_speed(request: Request, preset: str = Form(""), db: Session = Depends(get_db)):
    """Switch the global sync-speed preset (queue side panel's segmented
    control). Global on purpose: the hot lane is one worker for the whole
    process, so the fastest interest wins for everyone. Unknown preset values
    degrade to no-op (same spirit as the queue's filter params)."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    sync_control.set_speed_preset(preset)
    return templates.TemplateResponse(
        request,
        "_sync_speed_seg.html",
        {
            "sync_speed": SYNC_SPEED_PRESETS,
            "sync_speed_active": sync_control.get_speed_preset(),
        },
    )


@router.get("/search", response_class=HTMLResponse)
def get_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    results = []
    query_term = (q or "").strip()

    truncated = False
    if query_term:
        # Search in client_name, work_order_no, job_code, sum3d_id
        # Case-insensitive substring matching across all four fields
        # Той самий N+1, що й на видачі: рядок пошуку рендерить маркування.
        all_orders = db.scalars(
            select(Order).options(selectinload(Order.material))
        ).all()
        query_lower = query_term.lower()

        for order in all_orders:
            # Check if query appears in any of the four fields (case-insensitive)
            if any(
                (field and query_lower in (field or "").lower())
                for field in [
                    order.client_name,
                    order.work_order_no,
                    order.job_code,
                    order.sum3d_id,
                ]
            ):
                results.append(order)

        # Cap results at 100 and flag if truncated
        if len(results) > 100:
            truncated = True
            results = results[:100]

        # Attach folder info for display
        attach_export_folder_uris(db, results)
        attach_job_code_folder_uris(db, results)

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "query": query_term,
            "results": results,
            "focused_ids": focused_ids(db, user),
            "truncated": truncated,
            "user": user,
            "statuses": STATUSES,
        },
    )


def back_to_queue(request: Request) -> str:
    """Path+query of the queue page the request came from, for post-action
    redirects that must keep the operator's active filters. Falls back to "/"
    when there's no usable Referer. Scheme and host are discarded, so only a
    local path is ever returned (no open-redirect surface); a Referer pointing
    outside the queue root ("/") is ignored too."""
    referer = request.headers.get("referer")
    if not referer:
        return "/"
    parts = urlsplit(referer)
    if parts.path not in ("", "/"):
        return "/"
    return "/" + (f"?{parts.query}" if parts.query else "")


def synced_day_tabs(request: Request) -> set[str]:
    """The explicit sidebar day (?date=dd.mm.yy) the sync was launched from,
    as a set of sheet-tab titles to force-include. Empty when there's no valid
    date filter — the default three-day window then applies unchanged."""
    referer = request.headers.get("referer")
    if not referer:
        return set()
    date_values = parse_qs(urlsplit(referer).query).get("date", [])
    return {value for value in date_values if parse_sheet_tab(value) is not None}


@router.post("/sync/pause")
def toggle_sync_pause(request: Request, db: Session = Depends(get_db)):
    """Pause or resume ALL Google Sheet traffic (read AND write) from the web.

    The same switch the tray menu flips (app/sync_control.py) — one process, one
    flag. Admin-only: an accidental pause silently stops the queue from tracking
    the sheet, so it isn't an operator-level toggle. Returns to the queue with a
    toast; a banner there keeps an active pause visible so it's never forgotten."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    sync_control.set_paused(not sync_control.is_paused())
    paused = sync_control.is_paused()
    request.session["sync_flash"] = {
        "kind": "info",
        "message": (
            "Синхронізацію таблиці призупинено — система не читає й не пише таблицю."
            if paused
            else "Синхронізацію відновлено — читаю свіжу таблицю."
        ),
    }
    return RedirectResponse("/", status_code=303)


@router.post("/sheets/sync")
def sync_sheets(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    if sync_control.is_paused():
        request.session["sync_flash"] = {
            "kind": "info",
            "message": "Синхронізацію призупинено. Зніміть паузу, щоб синхронізувати.",
        }
        return RedirectResponse("/", status_code=303)

    # If the operator triggered the sync while viewing a specific day (the
    # sidebar "Дні" strip sets ?date=dd.mm.yy), force-include that tab so a
    # manual sync of an older day reconciles deletions there too — the periodic
    # window (yesterday/today/tomorrow) never revisits old tabs on its own.
    include_tabs = synced_day_tabs(request)

    try:
        summary = sync_google_sheets(db, include_tabs=include_tabs)
    except SheetSyncError as exc:
        request.session["sync_flash"] = {"kind": "error", "message": str(exc)}
    else:
        request.session["sync_flash"] = {
            "kind": "success",
            "message": summary_message(summary),
        }
    # Return to the exact queue view the operator synced from (period/ready/
    # source/date/sort filters live in the query string) instead of resetting
    # to bare "/". Only the path+query of a same-app Referer is used — scheme/
    # host are dropped, so this can't become an open redirect.
    return RedirectResponse(back_to_queue(request), status_code=303)


@router.post("/sheets/import-history")
def import_sheet_history(request: Request, db: Session = Depends(get_db)):
    """One-off «import the WHOLE sheet»: pull EVERY dated tab, not just the
    periodic today±1 window, so the queue's day-strip gains every historical
    day the sheet holds (arrows then page through them). Deliberately manual —
    it's a heavier run (one proxy read per tab) that the operator asks for once;
    the background sync stays fast. Admin + loopback, same gate as the queue's
    plain sync button."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    try:
        summary = sync_google_sheets(db, trigger="manual", full_history=True)
    except SheetSyncError as exc:
        request.session["sync_flash"] = {"kind": "error", "message": str(exc)}
    else:
        request.session["sync_flash"] = {
            "kind": "success",
            "message": "Історію таблиці імпортовано. " + summary_message(summary),
        }
    return RedirectResponse("/", status_code=303)
