"""Екран видачі — найболючіший ручний етап дня (CLAUDE.md §2, §9.4).

Три правила, виведені з реального процесу, і порушити їх = зламати роботу:
порядок списку фіксується на початку дня, «знайшов» і «видав» — ОДИН клік, і
жодного авто-зіставлення (спільного ключа між рядком таблиці й фізичною
коронкою не існує — розрізняють оком по STL).

Часткова видача — норма, а не виняток: цирконій іде через три пічки, які
відкриваються в різний час, тож роботи одного клієнта фізично виходять
порціями.
"""

import logging
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import sync_control
from app.client_matcher import match_client_name
from app.export_scanner import list_export_client_names_cached
from app.models import Client, ClientNameAlias, Order, StatusEvent
from app.order_folder import folder_to_file_uri
from app.parser import HEADER_ROWS
from app.queue_filters import (
    HANDOUT_SOURCE_FILTERS,
    count_client_groups_by_source,
    filter_client_groups_by_source,
)
from app.routers.deps import (
    SYNC_PAUSED_MSG,
    get_current_user,
    get_db,
    templates,
)
from app.services.clients import ensure_client_profiles, quantity_units
from app.services.handout import (
    handout_day_totals,
    HANDOUT_ALL_DAYS,
    entries_for_material,
    handout_client_matches,
    handout_day_options,
    handout_eligible_orders,
    handout_not_before,
    handout_select_day,
    matched_folders,
    scan_export_for_clients,
    scan_export_latest_for_clients,
)
from app.services.order_dates import parse_sheet_tab, sheet_order_key
from app.services.sheet_writeback import set_client_row_fill_background, write_sheet_fields
from app.settings_store import get_export_folder_path
from app.sheet_writer import apply_status_markers, clear_row_fills
from app.sheets import get_worksheet_by_name, open_spreadsheet
from app.stl_preview import build_preview_token

logger = logging.getLogger(__name__)

router = APIRouter()


def handout_context(request: Request, user, source: str, day: str, db: Session) -> dict:
    """Усе, що показує екран видачі.

    Винесено з роута, бо відмітка «знайдено» тепер підмінює список карток
    через HTMX і мусить будувати рівно те саме, що й повне відкриття екрана —
    інакше після галочки прогрес, кнопка «Видати N з M» чи позначка поточного
    клієнта розійшлися б із рештою сторінки."""
    if source not in HANDOUT_SOURCE_FILTERS:
        source = "all"

    today = date.today()
    eligible = handout_eligible_orders(db, today)

    # Day chips (14.08, 15.08, …): every past day that still has unissued
    # client works. `day` narrows the whole screen to that one day — the
    # operator hands out one day's furnace output at a time.
    handout_days = handout_day_options(eligible)
    selected_day = handout_select_day(handout_days, day)
    if selected_day is not None:
        shown = [o for o in eligible if parse_sheet_tab(o.sheet_tab) == selected_day]
        # Скільки робіт лишилось на інших днях — щоб замовчування «останній
        # день» ніколи не ховало хвіст мовчки.
        other_days_count = len(eligible) - len(shown)
        eligible = shown
    else:
        other_days_count = 0

    groups: dict[str, list[Order]] = {}
    for order in eligible:
        groups.setdefault(order.client_name, []).append(order)

    # Sheet order (day, then row position top-to-bottom), not DB insertion
    # order — the lab reads this as a rough readiness timeline (furnaces close
    # at different times through the day), so a client's own works AND the
    # card order itself both follow it, same as flipping through the table.
    for group_orders in groups.values():
        group_orders.sort(key=sheet_order_key)

    # `export` — шара Synology через SMB, де ціну диктує КІЛЬКІСТЬ звернень.
    # Повний обхід дерева тут коштував 65с (бойовий лог 25.08.26) і сторінка
    # не відкривалась. Тепер обхід ЛІНИВИЙ:
    #   рівень 1 (імена тек клієнтів) — один запит, потрібен для нечіткого
    #       зіставлення «ім'я в таблиці ↔ назва теки»;
    #   глибина — тільки для клієнтів, що реально на цьому екрані.
    # Робота стала пропорційна показаному (10-20 клієнтів), а не вмісту
    # сховища (сотні тек).
    _export_root = Path(get_export_folder_path(db))
    _scan_started = time.monotonic()
    folder_names = list_export_client_names_cached(_export_root)
    _not_before = handout_not_before(eligible)
    entries: list = []          # наповнюється нижче, після обходу
    # Every client on this screen gets a card (idempotent), so «Клієнти» and the
    # handout always show the same people and the folder binding is always one
    # click away. Cheap next to the export scan above.
    ensure_client_profiles(db, eligible)
    clients_by_name = {
        c.canonical_name.strip().casefold(): c.id
        for c in db.scalars(select(Client)).all()
        if c.canonical_name
    }
    client_names = [c for c in clients_by_name]

    def _client_id_for(name: str) -> int | None:
        """Exact fold first, then the same fuzzy matcher — the sheet spells one
        lab several ways and they must all reach the one card."""
        folded = (name or "").strip().casefold()
        if folded in clients_by_name:
            return clients_by_name[folded]
        hit = match_client_name(folded, client_names, {}).matched_folder_name
        return clients_by_name.get(hit) if hit else None

    matches = handout_client_matches(db, list(groups), folder_names)
    _folders = matched_folders(matches)
    scanned = scan_export_for_clients(_export_root, _folders, _not_before)
    # Тека прив'язана, а в вікні порожньо — значить файли скачали задовго до
    # фрезерування. Тоді дивимось найновіші партії клієнта без межі за датою:
    # це один scandir теки плюс захід у три найсвіжіші партії, а не повний
    # обхід усіх ~176. Бойовий випадок 28.08.26: «папку знайти не можу, хоча
    # вона є» (Светлана Криничко, робота 27.08, файли значно старіші).
    _empty = {name: folder for name, folder in _folders.items() if not scanned.get(name)}
    if _empty:
        for name, entries_ in scan_export_latest_for_clients(_export_root, _empty).items():
            scanned[name] = entries_

    client_groups = []
    for client_name, group_orders in groups.items():
        match = matches[client_name]
        export_entries = scanned.get(client_name, [])
        entries.extend(export_entries)
        for entry in export_entries:
            entry.folder_uri = folder_to_file_uri(entry.folder_path)
            entry.preview_token = build_preview_token(
                entry.folder_path, {"export": get_export_folder_path(db)}
            )
        # Per-row candidates: narrow the client's export folders to the ones
        # whose material matches THIS work's material_color, oldest-first. The
        # path carries no наряд/Sum3D ID (user decision 16.08.26: export stays
        # "нова папка"), so this is an ASSIST, not an exact bind — when several
        # works share a material the same folders show under each, and the
        # operator picks by eye (Sum3D ID + STL preview are their anchor).
        for order in group_orders:
            order.export_matches = entries_for_material(
                order.material_color, export_entries, parse_sheet_tab(order.sheet_tab)
            )
        # Теки, чий матеріал не збігся з жодним рядком, раніше показувались
        # окремим підвалом «Інші папки». Прибрано на прохання оператора
        # (28.08.26): на ранковій видачі це шум — звіряють коронку з STL за
        # рядком роботи, а не гортають чужі теки. Тека клієнта цілком лишається
        # одним кліком (`client_folder_uri`), якщо матеріал таки підписали не
        # так і теку треба відкрити руками.
        all_found = all(o.status in ("знайдено при видачі", "видано") for o in group_orders)
        # Client-level folder (the parent of the material folders) so the client
        # name itself opens the right place on disk, and a link to the client
        # card so an unbound client can be fixed once instead of every morning.
        # Тека клієнта береться з самого ЗІСТАВЛЕННЯ, а не з знайдених партій.
        # Раніше вона залежала від `export_entries`, тож клієнт із прив'язаною
        # текою, але без свіжих партій, отримував заклик «Прив'язати папку» —
        # екран казав «не прив'язано» там, де насправді «немає свіжих партій»
        # (бойовий випадок 28.08.26).
        client_folder_uri = None
        client_folder_token = None
        if match.matched_folder_name:
            client_folder = _export_root / match.matched_folder_name
            client_folder_uri = folder_to_file_uri(client_folder)
            # Токен, а не лише file://-посилання: браузер МОВЧКИ блокує перехід
            # на file:// зі сторінки на http, тому кнопка «Відкрити папку» досі
            # не робила нічого (бойовий випадок 28.08.26). Провідник відкриває
            # сервер через /open-folder, як це вже роблять прев'ю і черга.
            client_folder_token = build_preview_token(
                client_folder, {"export": get_export_folder_path(db)}
            )
        client_groups.append(
            {
                "client_name": client_name,
                "orders": group_orders,
                "match": match,
                "export_entries": export_entries,
                "all_found": all_found,
                "client_folder_uri": client_folder_uri,
                "client_folder_token": client_folder_token,
                "client_id": _client_id_for(client_name),
            }
        )

    # Cards themselves follow the same top-to-bottom principle, keyed off
    # each client's earliest (already-sorted) work.
    client_groups.sort(key=lambda g: sheet_order_key(g["orders"][0]))

    source_counts = count_client_groups_by_source(client_groups)
    client_groups = filter_client_groups_by_source(client_groups, source)
    handout_flash = request.session.pop("handout_flash", None)

    # Queue position + per-client totals. The position is THE anchor of the
    # screen: the operator works strictly in the order the clients were milled
    # (which is the sheet order these groups are already sorted by), so it is
    # numbered explicitly rather than left implicit in the scroll position.
    # `is_current` marks the first client not yet fully found — where the
    # operator is right now.
    current_marked = False
    for index, group in enumerate(client_groups, start=1):
        group["position"] = index
        group["works_count"] = len(group["orders"])
        group["units_total"] = sum(quantity_units(o.quantity) for o in group["orders"])
        group["found_count"] = sum(
            1 for o in group["orders"] if o.status in ("знайдено при видачі", "видано")
        )
        group["is_current"] = not group["all_found"] and not current_marked
        if group["is_current"]:
            current_marked = True

    done_groups = sum(1 for g in client_groups if g["all_found"])
    # Рахунок ЗА ОБРАНИЙ ДЕНЬ — прохання власника: яке число видане, за те
    # число й рахуємо. Так і виходить: `eligible` вище вже звужено до дня, а
    # `client_groups` — ще й до обраного джерела, тобто цифри описують рівно
    # те, що зараз на екрані, а не всю чергу.
    #
    # Клієнтів мало (десятки), робіт — більше, і саме роботи кажуть, скільки
    # ще фізично шукати в лотку: «3 / 11 клієнтів» нічого не каже про те, що в
    # тих восьми може бути і вісім робіт, і сорок.
    # Знаменники — ЗА ЦІЛИЙ ДЕНЬ, разом із уже виданим. Інакше вони
    # зменшуються протягом дня (видане випадає з `eligible`), і лічильник їде
    # НАЗАД після успішної видачі: «1 / 34 кл.» → «0 / 33 кл.». Найгірше, що
    # «X од.» при цьому виглядало як обсяг дня — число, яке диктують
    # керівництву, — хоча означало залишок.
    #
    # Один день = один знаменник, чисельник росте. Коли день не обрано (режим
    # «усі дні»), фіксувати нема чого — рахуємо показане, як раніше.
    # Передаємо ВЖЕ ВІДФІЛЬТРОВАНІ групи: знаменник мусить рахуватись із тієї
    # самої множини, що й чисельник, інакше при фільтрі «Пошта» шапка описувала
    # б обидва джерела, а коментар обіцяє протилежне.
    day_totals = handout_day_totals(db, selected_day, client_groups) if selected_day else {}
    if day_totals:
        total_groups_all = day_totals["clients"]
        done_groups = day_totals["clients_done"]
        total_works = day_totals["works"]
        found_works = day_totals["works_done"]
        total_units = day_totals["units"]
    else:
        total_groups_all = len(client_groups)
        total_works = sum(g["works_count"] for g in client_groups)
        found_works = sum(g["found_count"] for g in client_groups)
        total_units = sum(g["units_total"] for g in client_groups)
    # Clients with no bound export folder. Counted so the screen can say it ONCE
    # at the top instead of putting a warning chip on every card — with nothing
    # bound yet that was 34 amber calls-to-action, which reads as noise and
    # buries the one client the operator is actually on.
    unbound_count = sum(1 for g in client_groups if not g["client_folder_uri"])

    # Таймінг обходу сховища в лог: без нього причину «сторінка не
    # відкривається» доводиться вгадувати (так і сталось 27.08.26).
    logger.info(
        "Handout export scan: %d клієнтів на екрані, %d тек у сховищі, "
        "%d записів, партії від %s, %.2fс",
        len(groups), len(folder_names), len(entries),
        _not_before.date() if _not_before else "усі",
        time.monotonic() - _scan_started,
    )
    return {
            "page_title": "Ранкова видача",
            "user": user,
            "client_groups": client_groups,
            "source": source,
            "source_counts": source_counts,
            "handout_flash": handout_flash,
            "handout_days": [d.strftime("%d.%m.%y") for d in handout_days],
            "selected_day": selected_day.strftime("%d.%m.%y") if selected_day else "",
            # Те, що треба покласти в посилання/форми, щоб повернутись СЮДИ ж:
            # порожній рядок означав би «замовчування», а не «всі дні».
            "day_param": selected_day.strftime("%d.%m.%y") if selected_day else HANDOUT_ALL_DAYS,
            "other_days_count": other_days_count,
            "prev_day": adjacent_handout_day(handout_days, selected_day, -1),
            "next_day": adjacent_handout_day(handout_days, selected_day, +1),
            "day_window": handout_day_window(handout_days, selected_day),
            "done_groups": done_groups,
            "total_groups": total_groups_all,
            "total_works": total_works,
            "found_works": found_works,
            "total_units": total_units,
            # Розкладка з акаунта. Потрібна і фрагменту карток (покажчик дня
            # живе всередині нього, щоб оновлюватись разом із галочками), тому
            # їде контекстом, а не читається в шаблоні.
            "handout_layout": (user.handout_layout or "") if user else "",
            "unbound_count": unbound_count,
    }


@router.get("/handout", response_class=HTMLResponse)
def get_handout(
    request: Request, source: str = "all", day: str = "", db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "handout.html", handout_context(request, user, source, day, db)
    )


def handout_cards_response(request: Request, user, source: str, day: str, db: Session):
    """Лише список карток — відповідь на HTMX-відмітку.

    Сторінка НЕ перезавантажується, тому екран лишається рівно там, де
    оператор його прокрутив. Раніше кожна галочка йшла звичайною формою з
    редіректом, і після кожної екран смикався на початок — на видачі, де
    йдуть списком згори вниз і клацають підряд, це збивало саме те, заради
    чого екран і робився."""
    # oob_kpi лише тут: у повній сторінці той самий партіал уже стоїть у шапці,
    # і друга копія дала б дубльований id (див. коментар у _handout_cards.html).
    context = handout_context(request, user, source, day, db)
    context["oob_kpi"] = True
    return templates.TemplateResponse(request, "_handout_cards.html", context)


HANDOUT_DAY_WINDOW = 3


def handout_day_window(days: list, selected) -> list[dict]:
    """The few days shown in the pager, newest-last, with the selected one marked.

    A single date with ‹ › arrows hid where the operator was in the week; a wall
    of 30+ chips drowned the screen. Three days is the middle: the current day
    plus its neighbours, so stepping back a day is one click and the position is
    visible. Anchored on the selected day (or the newest one when the screen
    shows all days), and clamped so the window stays full at either end."""
    if not days:
        return []
    size = min(HANDOUT_DAY_WINDOW, len(days))
    anchor = days.index(selected) if selected in days else len(days) - 1
    start = max(0, min(anchor - size // 2, len(days) - size))
    return [
        {"value": d.strftime("%d.%m.%y"), "label": d.strftime("%d.%m"), "active": d == selected}
        for d in days[start:start + size]
    ]


def adjacent_handout_day(days: list, selected, step: int) -> str:
    """Neighbouring day for the ‹ › pager, or "" at the ends. Replaces the wall
    of 30+ day chips: the operator hands out one day at a time and steps between
    them, so only the neighbours need to be one click away."""
    if not days:
        return ""
    if selected is None:
        # No day chosen: ‹ opens the newest day, › stays inert.
        return days[-1].strftime("%d.%m.%y") if step < 0 else ""
    try:
        index = days.index(selected)
    except ValueError:
        return ""
    target = index + step
    if 0 <= target < len(days):
        return days[target].strftime("%d.%m.%y")
    return ""


def handout_back_url(source: str, day: str) -> str:
    """Rebuild the exact handout view (source tab + day chip) the operator was
    on, so a mark/unmark POST returns them there instead of the unfiltered
    all-days list."""
    params: list[str] = []
    if source and source in HANDOUT_SOURCE_FILTERS and source != "all":
        params.append(f"source={source}")
    if day:
        params.append(f"day={day}")
    return "/handout" + ("?" + "&".join(params) if params else "")


@router.post("/orders/{order_id}/mark-found")
async def mark_found(
    request: Request,
    order_id: int,
    source: str = Form("all"),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    order.status = "знайдено при видачі"
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username)
    )
    db.commit()
    # Found = physically located → clear the sheet's blue "pending" fill to
    # white, per the lab's colour convention. У фоні: галочка має ставитись
    # миттєво, бо оператор клацає їх підряд.
    # Пауза синку означає «жодного запису в таблицю» — інакше оператор, що
    # поставив паузу перед чисткою таблиці, тихо отримав би десятки
    # перефарбувань повз неї. Решта write-роутів гейт мають, ці два не мали.
    if not sync_control.is_paused():
        set_client_row_fill_background(order.id, blue=False)
    # HTMX-клік підмінює лише список карток — сторінка не перезавантажується,
    # тож скрол лишається там, де оператор його поставив. Редірект лишається
    # для звичайної форми (без JS) і для прямих переходів.
    if request.headers.get("HX-Request"):
        return handout_cards_response(request, user, source, day, db)
    return RedirectResponse(handout_back_url(source, day), status_code=303)


@router.post("/orders/{order_id}/unmark-found")
async def unmark_found(
    request: Request,
    order_id: int,
    source: str = Form("all"),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
    """Undo an accidental "знайдено" click: revert the order to pending and
    repaint the sheet row blue so the next sync doesn't read the cleared fill
    as issued. Refuses to touch an already-issued ("видано") order."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status != "знайдено при видачі":
        return RedirectResponse(handout_back_url(source, day), status_code=303)

    # Повертаємо ПОПЕРЕДНІЙ статус, а не «нове». Робота на видачі за
    # визначенням уже відфрезерована, тож скидання в «нове» означало б, що
    # випадковий клік по галочці й назад показує її невиготовленою — рівно
    # той стан, який §5 називає «записалась, а не зробилась».
    previous = db.scalars(
        select(StatusEvent)
        .where(
            StatusEvent.order_id == order.id,
            StatusEvent.status != "знайдено при видачі",
        )
        .order_by(StatusEvent.id.desc())
    ).first()
    order.status = previous.status if previous else "відфрезеровано"
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username)
    )
    db.commit()
    # Un-found = back to pending → repaint the blue fill so sheet state and
    # portal status stay consistent (a white fill + "нове" would otherwise be
    # read as issued on the next sync). Так само у фоні, і так само під паузою.
    if not sync_control.is_paused():
        set_client_row_fill_background(order.id, blue=True)
    # HTMX-клік підмінює лише список карток — сторінка не перезавантажується,
    # тож скрол лишається там, де оператор його поставив. Редірект лишається
    # для звичайної форми (без JS) і для прямих переходів.
    if request.headers.get("HX-Request"):
        return handout_cards_response(request, user, source, day, db)
    return RedirectResponse(handout_back_url(source, day), status_code=303)


@router.post("/handout/issue-group")
async def issue_handout_group(
    request: Request,
    client_name: str = Form(...),
    source: str = Form("all"),
    day: str = Form(""),
    db: Session = Depends(get_db),
):
    """One click on a handout card's "Видати" button closes the whole client
    group: every found-but-not-yet-issued order flips to "видано" (mirroring
    the single-order status route), and every sheet_client row's blue
    "pending" fill is cleared back to white in ONE batched sheet call — the
    counterpart of the lab's own manual "clear the blue = issued" convention.

    Re-derives the group server-side from client_name (never trusts a client
    id list from the form) and issues EXACTLY the orders already marked
    "знайдено при видачі"/"видано", leaving the rest of the card open.

    Часткова видача — норма процесу, не виняток (CLAUDE.md §2): цирконій іде
    через три пічки, які відкриваються ~9:00, в обід і під вечір, тож роботи
    одного клієнта фізично виходять у різний час. Раніше тут стояв gate
    all_found, і клієнт із 52 роботами не міг отримати «Видати» жодного разу
    за день — оператор мусив або чекати до вечора, або обходити портал через
    Google-таблицю."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    # Handout clears the blue "pending" fill in the sheet — a table write, so
    # it's refused while paused; the operator issues after resume.
    if sync_control.is_paused():
        request.session["toast_flash"] = {"message": SYNC_PAUSED_MSG, "kind": "info"}
        return RedirectResponse(handout_back_url(source, day), status_code=303)

    today = date.today()
    candidates = db.scalars(
        select(Order).where(Order.client_name == client_name, Order.status != "видано")
    ).all()
    group_orders = [
        o for o in candidates
        if (d := parse_sheet_tab(o.sheet_tab)) is not None and d < today
    ]
    # When the handout screen is filtered to one day (day chips), the card
    # the operator sees — and therefore what "Видати" closes — is that day's
    # works only; the client's other days stay open.
    selected_day = parse_sheet_tab(day) if day else None
    back_url = handout_back_url(source, day)
    if selected_day is not None:
        group_orders = [
            o for o in group_orders if parse_sheet_tab(o.sheet_tab) == selected_day
        ]
    if not group_orders:
        return RedirectResponse(back_url, status_code=303)
    # Видаємо рівно те, що оператор уже знайшов. Решта лишається в картці.
    group_orders = [
        o for o in group_orders if o.status in ("знайдено при видачі", "видано")
    ]
    if not group_orders:
        request.session["handout_flash"] = {
            "kind": "info",
            "message": "Нічого видавати: жодну роботу цього клієнта не позначено «знайдено».",
        }
        return RedirectResponse(back_url, status_code=303)

    actor = user.full_name or user.username
    sync_error: str | None = None
    clear_targets: list[tuple[str, int]] = []  # (sheet_tab, row_number)
    for order in group_orders:
        order.status = "видано"
        sheet_fields = apply_status_markers(order, "видано", actor=actor)
        db.add(
            StatusEvent(order_id=order.id, operator_id=user.id, status="видано", actor=user.username)
        )
        err = write_sheet_fields(db, order, sheet_fields)
        sync_error = sync_error or err
        if order.source == "sheet_client" and order.sheet_tab and order.row_number is not None:
            clear_targets.append((order.sheet_tab, order.row_number))

    if clear_targets:
        try:
            spreadsheet = open_spreadsheet(db=db)
            rows_by_sheet_id: list[tuple[int, int]] = []
            for sheet_tab, row_number in clear_targets:
                worksheet = get_worksheet_by_name(spreadsheet, sheet_tab)
                if worksheet is not None:
                    rows_by_sheet_id.append((worksheet.id, row_number + HEADER_ROWS))
            clear_row_fills(spreadsheet, rows_by_sheet_id)
        except Exception as exc:  # noqa: BLE001 — never fail the видано status over this
            logger.exception("Failed to clear blue fill for handout group %r", client_name)
            sync_error = sync_error or str(exc)

    db.commit()

    if sync_error:
        request.session["handout_flash"] = {
            "kind": "error",
            "message": f"Статус видано, але запис у таблицю не пройшов: {sync_error}",
        }
    # Той самий контракт, що й у галочки: HTMX підмінює лише список карток.
    # Раніше видача клієнта перезавантажувала сторінку, кидала оператора на
    # початок списку й скидала фільтр джерела — тобто ламала правило «порядок
    # фіксується на початку дня», заради якого галочку вже полагодили.
    if request.headers.get("HX-Request"):
        return handout_cards_response(request, user, source, day, db)
    return RedirectResponse(back_url, status_code=303)


@router.post("/handout/confirm-alias")
async def confirm_alias(
    request: Request,
    sheet_name: str = Form(...),
    export_folder_name: str = Form(...),
    db: Session = Depends(get_db),
):
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    existing = db.scalar(select(ClientNameAlias).where(ClientNameAlias.sheet_name == sheet_name))
    if existing is not None:
        existing.export_folder_name = export_folder_name
        existing.confirmed = True
        existing.confirmed_at = datetime.now()
    else:
        db.add(
            ClientNameAlias(
                sheet_name=sheet_name,
                export_folder_name=export_folder_name,
                confirmed=True,
                confirmed_at=datetime.now(),
            )
        )
    db.commit()

    return RedirectResponse("/handout", status_code=303)
