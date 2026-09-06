"""Черга робіт: як з параметрів фільтра народжується вміст екрана.

Тут живе ВСЯ доменна частина основного екрана (CLAUDE.md §9.1) — перевірка
параметрів, вибірка робіт, бакети днів, фільтри-чіпи, сортування, шпильки
«мої зараз», KPI-картки й смужка днів. Request сюди не потрапляє: роут
`app/routers/queue.py::get_queue` лише розбирає рядок запиту, кличе
`build_queue_view` і віддає шаблон (CLAUDE.md §14, «бачить Request → роутер»).

Чому окремий файл, а не `app/services/queue.py`: той модуль тягне `deps.py`
(через `is_rush_comment`), і додавати туди залежності від печей, верстатів і
сканера мережевої шари означало б вантажити їх на кожен імпорт залежностей.
Тут же — навпаки, збірка всього контексту разом.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import perf
from app import sync_control
from app.business_day import business_today
from app.mail_sync_service import is_mail_sync_running
from app.models import EmailMessage, Order, SavedQueueView
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
from app.services.config_state import (
    mail_preview_roots,
    mail_trusted_roots,
    sheets_configured,
)
from app.services.focus import count as focus_count, ranks as focus_ranks
from app.services.furnace import (
    all_idle as furnaces_all_idle,
    configured_targets as furnace_targets,
    strip_cards as furnace_cards,
    strip_summary as furnace_summary,
)
from app.services.machines import machine_side_context, milling_now, sisma_context
from app.services.order_dates import order_date, parse_sheet_tab
from app.services.queue import (
    QUEUE_SORT_FIELDS,
    RETENTION_DAYS,
    date_window,
    handout_pending_client_count,
    queue_handout_summary,
    queue_sort_key,
    queue_sync_summary,
    queue_week_summary,
    sort_orders_by_column,
)
from app.services.shift import open_notes as open_shift_notes
from app.services.system_load import snapshot as system_load_snapshot
from app.sheet_sync_service import (
    header_mismatch_pending,
    is_sheet_sync_running,
    mass_vanish_pending,
)
from app.statuses import STATUSES, is_overdue
from app.sync_control import SYNC_SPEED_PRESETS, get_sync_speed, record_viewed_day
from app.sync_heartbeat import sync_status_pair

logger = logging.getLogger(__name__)


def live_sync_status(db: Session) -> dict:
    """sync_status_pair overlaid with the LIVE «running now» / «paused» flags.

    sync_status_pair reports the last completed tick's outcome; the queue's
    status dot also needs to know a sync is happening *right now* (to pulse)
    and whether reading/writing the sheet is paused. Both are cheap in-memory
    reads (the sync lock, the pause flag), so they layer on here rather than
    inside the pure formatter."""
    pair = sync_status_pair(db, datetime.now())
    paused = sync_control.is_paused()
    pair["sheet"]["running"] = is_sheet_sync_running()
    pair["sheet"]["paused"] = paused
    pair["mail"]["running"] = is_mail_sync_running()
    return pair


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


@dataclass(frozen=True)
class QueueView:
    """Готовий екран черги: який шаблон малювати і чим його годувати.

    Шаблон обирає сервіс, бо вибір диктує доменний режим (полл рядків проти
    повного рендера), а не HTTP. Роут лише кличе `TemplateResponse`.
    """

    template: str
    context: dict


def build_queue_view(
    db: Session,
    user,
    *,
    period: str = "today",
    ready: str = "all",
    source: str = "all",
    overdue: str = "0",
    mine: str = "",
    date_param: str = "",
    date_page: int | None = None,
    sort: str = "",
    sort_dir: str = "asc",
    partial: str = "",
    focus: str = "",
) -> QueueView:
    """Зібрати екран черги для цього оператора й цього набору фільтрів.

    Параметри приходять уже як прості рядки — рівно так, як їх віддає FastAPI,
    — тож функцію можна кликати й без HTTP. Перевірка значень теж тут: правило
    «невідоме значення тихо падає в замовчування» — доменне, а не транспортне.
    """
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

    # Fetch active orders (eager-load material for the queue's material badge).
    # `archived_at IS NULL` стоїть у SQL, а не лише в Python нижче: архів росте
    # з кожним місяцем, і без цієї умови кожне відкриття черги піднімало в
    # памʼять УСЮ історію, щоб одразу її ж і відкинути. Відсів за вікном
    # retention лишається в Python — бізнес-дата виводиться з `sheet_tab`
    # (рядок «дд.мм.рр»), а не зі стовпця, тож у SQL її не порівняти.
    _t_sql = time.monotonic()
    all_orders = db.scalars(
        select(Order)
        .options(selectinload(Order.material))
        .where(Order.archived_at.is_(None))
        .order_by(Order.id.desc())
    ).all()
    _sql_seconds = time.monotonic() - _t_sql
    perf.add("sql", _sql_seconds)

    # Define date boundaries
    today = business_today()
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

    # Іконки папок (export + файли техніків) годуються скануванням МЕРЕЖЕВОЇ
    # шари — на бойовому ПК це 2-3с холодного звернення до Synology, і саме
    # воно робило відкриття черги повільним (заміряно /diag/perf 04.09.26:
    # share:tech ~2.9с, разом зі скан:sum3d ~5с на повний рендер). Сторінка ж
    # їх не потребує, щоб намалюватись: рядки, статуси й ГОТОВНІСТЬ читаються
    # з БД (job_code — колонка, не скан). Тому:
    #   • повний рендер сторінки їх НЕ робить (швидкий перший малюнок);
    #   • полл рядків (partial=rows) — робить, і саме він, стрельнувши одразу
    #     після завантаження (hx-trigger="load" у queue.html), домальовує
    #     іконки за мить і далі тримає кеш теплим кожні 15с.
    # Готовність (_has_path) від сканів не залежить, тож фільтр і лічильники
    # лишаються точними навіть на першому, «голому» рендері.
    if partial == "rows":
        with perf.span("share:export"):
            attach_export_folder_uris(db, orders)
        with perf.span("share:tech"):
            attach_job_code_folder_uris(db, orders)
    perf.note_rows(len(orders))

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
    sync_status = live_sync_status(db)

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
    # params (not request.query_params) so it also works when build_queue_view
    # is called directly in tests, and reflects clamped/validated values.
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

    # Лоток Sum3D у шапці самополлиться через /sum3d/tray (див. _sum3d_tray.html)
    # і сам сканує теку — тут його НЕ скануємо. Раніше повний рендер робив цей
    # скан лише щоб засіяти перший малюнок лотка, і платив за нього 2-3с на
    # мережевій теці (заміряно /diag/perf 04.09.26). Лоток тепер починає
    # порожнім і підтягує себе одразу після завантаження (hx-trigger="load").
    # Порожній список — валідний перший стан, а не «немає проєктів».
    _s3 = None

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
            # Банер масового видалення: вкладки, чиї видалення тримає запобіжник
            # (schema-guard), з кнопкою «Звірити видалення». Порожньо = банера нема.
            "mass_vanish": mass_vanish_pending(),
            # Зсув колонок у таблиці — той самий банер, той самий сенс:
            # синк свідомо стоїть, поки людина не гляне (синк H-6).
            "header_mismatch": header_mismatch_pending(),
            "has_any_orders": bool(all_orders),
            "sheets_configured": sheets_configured(db),
            # Флеші живуть у сесії запиту, тобто на HTTP-рівні — їх домішує роут
            # (`get_queue`). Ключі оголошені тут, щоб шаблон ніколи не побачив
            # відсутнього імені, якщо контекст зібрали без роута (тести).
            "sync_flash": None,
            "toast_flash": None,
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
            # Ті самі два поля, що й у роутів /furnaces/strip і /furnaces/side:
            # сторінка черги вкладає обидва партіали своїм контекстом, і без
            # них секція одразу після рестарту писала «печей не налаштовано»,
            # хоча вони налаштовані — просто ще не опитані.
            "furnaces_configured": len(furnace_targets(db)),
            "furnaces_all_idle": furnaces_all_idle(_furnace_cards),
            # Верстати для віджета в бічній панелі (_machine_side.html) —
            # перший рендер; далі секція оновлює себе через /machines/side.
            # Контекст будує ТОЙ САМИЙ machine_side_context, що й роут, щоб
            # два входи не розійшлись (урок віджета пічок).
            **machine_side_context(db),
            # Той самий вхід, що й роут /machines/sisma — див. _sisma_widget.html.
            **sisma_context(db),
            # Жива стрічка навантаження — перший рендер; далі #system-load
            # оновлює себе через /system/load. Стан лише з памʼяті.
            "load": system_load_snapshot(),
            # Мітки «мої зараз» — персональні, тому контекст, а не глобал.
            # ОБИДВІ гілки (сторінка й partial=rows) читають цей самий
            # словник: якби полл рахував інакше, мітки зникали б кожні 15с.
            "focused_ids": my_focus,
            # Що ЗАРАЗ фрезерується (Sum3D ID → верстат+відсоток). Мусить бути
            # в ОБОХ гілках — сторінці й partial=rows, — інакше підсвітка
            # зникала б на першому тіку полла (той самий урок, що focused_ids).
            "milling": milling_now(),
            # Найновіший захоплений Sum3D ID — для «привида» в пришпиленому
            # рядку. В ОБОХ гілках (сторінка й partial=rows), інакше підказка
            # зникала б на першому тіку полла.
            "sum3d_latest": _s3[0].sum3d_id if _s3 else None,
            # Підказка показується РІВНО В ОДНОМУ рядку — тому, що пришпилений
            # ОСТАННІМ: це та робота, яку оператор щойно взяв і саме для неї
            # створив проєкт. У всіх пришпилених одразу вона була б шумом і,
            # гірше, запрошенням вписати один ID у кілька робіт.
            #
            # Рахуємо з ТОГО САМОГО `my_focus_rank`, що й `focused_ids` вище
            # (CLAUDE.md §14: набір рахується в одному місці) — раніше тут був
            # другий виклик focus_ranks, тобто зайвий запит із ризиком
            # розійтись із підсвіткою.
            "sum3d_ghost_order_id": (
                max(my_focus_rank, key=my_focus_rank.get) if my_focus_rank else None
            ),
            "focus_count": focus_count(db, user),
            "focus_mine": focus_mine,
            "selected_date": selected_date,
            "date_tabs": date_tabs,
            "date_page": current_date_page,
            "total_date_pages": total_date_pages,
            "sort": sort,
            "sort_dir": sort_dir,
            "rows_qs": rows_qs,
            # Той самий набір фільтрів у КАНОНІЧНІЙ формі — рівно те, що
            # лягає у збережений вигляд. Порівнянням рядків смуга
            # підсвічує вигляд, який зараз і застосований; окремий ключ,
            # а не rows_qs, бо rows_qs несе й невалідний ?date, як його
            # набрали в адресі.
            "view_qs": normalize_view_query(rows_qs),
            "sync_speed": SYNC_SPEED_PRESETS,
            "sync_speed_active": sync_control.get_speed_preset(),
            "sync_screen_seconds": get_sync_speed()["screen"],
            "viewed_tab": viewed_day.strftime("%d.%m.%y") if viewed_day else "",
            "sync_paused": sync_control.is_paused(),
            "focus_order_id": focus.strip() if focus.strip().isdigit() else "",
    }

    record_viewed_day(viewed_day)

    # Розбивка часу, коли рендер справді довгий. Поріг у секунду, щоб рядок не
    # з'являвся на кожному тіку полла (черга оновлюється кожні 15 с). Без цієї
    # розбивки в логу лишалось тільки «Slow request: GET / took 4.17s», і
    # причину доводилось вгадувати з іншого ПК (скарга власника 03.09.26:
    # «перемикання між вкладками ~5 секунд»).
    # Скан Sum3D і мережеві скани іконок пішли з повного рендера (лоток і рядки
    # довантажуються самі), тож тут лишається SQL — єдине, що повний рендер
    # ще робить синхронно. Повна розкладка будь-якого запиту — на /diag/perf.
    if _sql_seconds >= 1.0:
        logger.info(
            "Queue render: %d робіт, SQL %.2fс", len(all_orders), _sql_seconds,
        )

    # The screen poll asks for just the rows block; everything else (sidebar
    # counts, KPIs) refreshes on a full navigation or a manual sync.
    if partial == "rows":
        return QueueView("_queue_rows.html", context)

    # Моно-лоток Sum3D — лише для повного рендера (у шапці, поза #queue-rows).
    context["sum3d_projects"] = _s3
    # Збережені вигляди теж лише тут: смуга фільтрів стоїть ВИЩЕ
    # `.worklayout`, полл рядків її не свапає — тягнути їх кожні 15 с
    # означало б зайвий запит заради розмітки, якої в відповіді немає.
    context["saved_views"] = saved_views(db, user)
    return QueueView("queue.html", context)


# ── Збережені вигляди черги ────────────────────────────────────────────────
#
# Вигляд = ім'я + рядок GET-параметрів, який уже розуміє `build_queue_view`
# вище. Свідомо НЕ окремий формат: черга фільтрується параметрами адреси, і
# другий, паралельний опис фільтрів розійшовся б з ним на першій же правці —
# збережені вигляди тихо почали б показувати не те. Тому застосування вигляду
# — це просто перехід на `/?<query>`, без жодної нової гілки фільтрації.

# Назва мусить влазити в пігулку смуги; довша ламала б перенос рядків.
VIEW_NAME_MAX = 60
# Смуга фільтрів і так розросталась на два-три рядки (скарга власника
# 30.08.26) — стеля тримає її в межах одного-двох.
MAX_SAVED_VIEWS = 12

_PERIODS = ("today", "yesterday", "tomorrow", "earlier")


class SavedViewError(ValueError):
    """Причина відмови, яку видно операторові прямо в смузі фільтрів."""


def normalize_view_query(raw: str) -> str:
    """Звести рядок фільтрів до канонічного вигляду, викинувши все чуже.

    Ключ, якого черга не знає (або вже не знає — фільтр прибрали), просто
    зникає; значення поза допустимим набором падає в замовчування. Тому
    вигляд, збережений півроку тому, відкриє чергу, а не 500-ту: правило
    «невідоме значення тихо падає в замовчування» тут те саме, що в
    `build_queue_view`.

    Порядок ключів — рівно той, що в `rows_qs`. Це не косметика: рівність
    рядків і є ознакою «цей вигляд зараз застосований», і смуга підсвічує
    активний без жодного розбору параметрів у шаблоні.
    """
    values = parse_qs(raw or "")

    def first(key: str) -> str:
        got = values.get(key) or []
        return got[0].strip() if got else ""

    period = first("period")
    if period not in _PERIODS:
        period = "today"
    ready = first("ready")
    if ready not in READY_FILTERS:
        ready = "all"
    source = first("source")
    if source not in SOURCE_FILTERS:
        source = "all"

    items: list[tuple[str, str]] = []
    if first("overdue") == "1":
        items.append(("overdue", "1"))
    items += [("period", period), ("ready", ready), ("source", source)]
    if first("mine") == "1":
        items.append(("mine", "1"))
    day = first("date")
    if parse_sheet_tab(day) is not None:
        page = first("date_page")
        items.append(("date", day))
        items.append(("date_page", page if page.isdigit() else "0"))
    sort = first("sort")
    if sort in QUEUE_SORT_FIELDS:
        items.append(("sort", sort))
        items.append(("dir", "desc" if first("dir") == "desc" else "asc"))
    return urlencode(items)


def saved_views(db: Session, user) -> list[SavedQueueView]:
    """Вигляди ЦЬОГО оператора, у порядку смуги.

    `user_id` в умові — не оптимізація, а сама приватність: іншого входу до
    таблиці немає, тож фільтр тут і є гарантією, що чужий набір не покажеться.
    """
    return list(
        db.scalars(
            select(SavedQueueView)
            .where(SavedQueueView.user_id == user.id)
            .order_by(SavedQueueView.position, SavedQueueView.id)
        ).all()
    )


def own_view(db: Session, user, view_id: int) -> SavedQueueView | None:
    """Вигляд оператора за id — або None.

    `user_id` знову В УМОВІ, а не перевіркою після вибірки: підставивши чужий
    номер в адресу, оператор не має ні прочитати, ні перейменувати, ні
    видалити чужий вигляд.
    """
    return db.scalars(
        select(SavedQueueView).where(
            SavedQueueView.id == view_id,
            SavedQueueView.user_id == user.id,
        )
    ).first()


def _clean_name(name: str) -> str:
    """Назва без крайніх і подвійних пробілів, обрізана під ширину пігулки."""
    return " ".join((name or "").split())[:VIEW_NAME_MAX]


def save_view(db: Session, user, name: str, query: str) -> SavedQueueView:
    """Зберегти поточний набір фільтрів під назвою."""
    clean = _clean_name(name)
    if not clean:
        raise SavedViewError("Дай виглядові назву — інакше в смузі його не впізнати.")
    existing = saved_views(db, user)
    if len(existing) >= MAX_SAVED_VIEWS:
        raise SavedViewError(
            f"Більше {MAX_SAVED_VIEWS} виглядів у смугу не влізе — прибери зайвий."
        )
    if any(view.name.casefold() == clean.casefold() for view in existing):
        raise SavedViewError(f"Вигляд «{clean}» уже збережено.")
    view = SavedQueueView(
        user_id=user.id,
        name=clean,
        query=normalize_view_query(query),
        # Новий вигляд стає В КІНЕЦЬ смуги: жоден уже збережений не рухається
        # (той самий урок, що зі шпильками «мої зараз»).
        position=(existing[-1].position + 1) if existing else 0,
        created_at=datetime.now(),
    )
    db.add(view)
    db.commit()
    return view


def rename_view(db: Session, user, view_id: int, name: str) -> SavedQueueView:
    """Перейменувати СВІЙ вигляд."""
    view = own_view(db, user, view_id)
    if view is None:
        raise SavedViewError("Такого вигляду немає.")
    clean = _clean_name(name)
    if not clean:
        raise SavedViewError("Порожня назва — вигляд лишився як був.")
    clash = any(
        other.id != view.id and other.name.casefold() == clean.casefold()
        for other in saved_views(db, user)
    )
    if clash:
        raise SavedViewError(f"Вигляд «{clean}» уже збережено.")
    view.name = clean
    db.commit()
    return view


def delete_view(db: Session, user, view_id: int) -> bool:
    """Прибрати СВІЙ вигляд. False — такого вигляду в цього оператора немає."""
    view = own_view(db, user, view_id)
    if view is None:
        return False
    db.delete(view)
    db.commit()
    return True
