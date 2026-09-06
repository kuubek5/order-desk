"""Черга робіт — основний екран, за яким оператор проводить 90% часу.

Показує всі незавершені роботи обох джерел (лабораторія і пошта) незалежно
від дня, з фільтрами-чіпами, смужкою днів і бічною панеллю стану синку.

Тут же ручні дії над самою синхронізацією: пауза, разовий синк і одноразовий
імпорт усієї історії таблиці.
"""

import json
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from app import sync_control
from app.models import Order
from app.order_folder import (
    attach_export_folder_uris,
    attach_job_code_folder_uris,
)
from app.routers.deps import get_current_user, login_redirect, get_db, templates
from app.services.system_load import snapshot as system_load_snapshot
from app.services.config_state import sheets_configured
from app.services.order_dates import parse_sheet_tab
from app.services.sum3d_capture import scan_projects as scan_sum3d_projects
from app.settings_store import get_sum3d_projects_path
from app.services.focus import focused_ids
from app.services.queue import queue_sync_summary
# Уся доменна збірка екрана черги переїхала в сервіс (CLAUDE.md §14).
# `live_sync_status` лишається доступним і як атрибут ЦЬОГО модуля: його
# використовує роут /sheets/state нижче, а app/routers/deps.py імпортує його
# саме звідси.
from app.services.queue_view import (
    SavedViewError,
    build_queue_view,
    delete_view,
    live_sync_status,
    normalize_view_query,
    rename_view,
    save_view,
    saved_views,
)
from app.sheet_sync_service import (
    SheetSyncError,
    header_mismatch_pending,
    mass_vanish_pending,
    pop_import_flash,
    start_background_import,
    summary_message,
    sync_google_sheets,
)
from app.statuses import STATUSES
from app.sync_control import SYNC_SPEED_PRESETS

router = APIRouter()


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
    # Скільки рядків малювати: «Показати ще» під таблицею шле більший limit
    # (сторінка рядків — app/services/queue_view.py, QUEUE_ROWS_PAGE).
    limit: int | None = None,
    db: Session = Depends(get_db),
):
    """Тонкий роут: розібрати рядок запиту, покликати сервіс, віддати шаблон.

    Уся доменна частина екрана (перевірка фільтрів, вибірка, бакети днів,
    сортування, шпильки «мої зараз», KPI, смужка днів) живе в
    `app/services/queue_view.py::build_queue_view` — CLAUDE.md §14: що бачить
    Request, те роутер; решта — сервіс.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    view = build_queue_view(
        db,
        user,
        period=period,
        ready=ready,
        source=source,
        overdue=overdue,
        mine=mine,
        date_param=date_param,
        date_page=date_page,
        sort=sort,
        sort_dir=sort_dir,
        partial=partial,
        focus=focus,
        limit=limit,
    )

    # Флеші — єдине, що лишилось на цьому боці: вони живуть у сесії ЗАПИТУ,
    # тобто на HTTP-рівні, і сервіс про неї не знає. Знімаємо їх ЛИШЕ на
    # повному рендері: 15-секундний полл (partial="rows") зʼїв би повідомлення
    # раніше, ніж оператор дійшов би до сторінки, на якій воно має з'явитись.
    if partial != "rows":
        view.context["sync_flash"] = request.session.pop("sync_flash", None)
        view.context["toast_flash"] = request.session.pop("toast_flash", None)

    return templates.TemplateResponse(request, view.template, view.context)


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


def _views_panel(
    request: Request,
    db: Session,
    user,
    current: str,
    *,
    error: str | None = None,
    open_form: str = "",
    form_name: str = "",
) -> HTMLResponse:
    """Перемалювати смугу «Вигляди» — єдина відповідь усіх трьох дій нижче.

    Свап іде outerHTML по `#queue-views`, тому сторінка не перезавантажується
    й прокрутка черги лишається на місці. `current` — рядок фільтрів, на якому
    оператор зараз стоїть: він приходить з форми, бо смуга не знає адреси
    сторінки, і нормалізується тут, щоб підсвітка активного вигляду порівнювала
    однакові форми запису.
    """
    return templates.TemplateResponse(
        request,
        "_queue_views.html",
        {
            "user": user,
            "saved_views": saved_views(db, user),
            "view_qs": normalize_view_query(current),
            "views_error": error,
            # Яка форма лишається розгорнутою після відповіді: "" — жодна,
            # "new" — «Зберегти поточний», інакше id вигляду. Потрібно рівно
            # для помилки: інакше свап зітер би набране ім'я, і оператор
            # набирав би його заново, не зрозумівши, що саме не так.
            "views_open_form": open_form,
            "views_form_name": form_name,
        },
    )


@router.post("/queue/views", response_class=HTMLResponse)
def create_queue_view(
    request: Request,
    name: str = Form(""),
    current: str = Form(""),
    db: Session = Depends(get_db),
):
    """Зберегти поточний набір фільтрів черги як іменований вигляд."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    try:
        save_view(db, user, name, current)
    except SavedViewError as exc:
        return _views_panel(
            request, db, user, current,
            error=str(exc), open_form="new", form_name=name,
        )
    return _views_panel(request, db, user, current)


@router.post("/queue/views/{view_id}/rename", response_class=HTMLResponse)
def rename_queue_view(
    request: Request,
    view_id: int,
    name: str = Form(""),
    current: str = Form(""),
    db: Session = Depends(get_db),
):
    """Перейменувати свій вигляд. Чужий id сюди не пролазить — `rename_view`
    шукає рядок разом із `user_id`."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    try:
        rename_view(db, user, view_id, name)
    except SavedViewError as exc:
        return _views_panel(
            request, db, user, current,
            error=str(exc), open_form=str(view_id), form_name=name,
        )
    return _views_panel(request, db, user, current)


@router.post("/queue/views/{view_id}/delete", response_class=HTMLResponse)
def remove_queue_view(
    request: Request,
    view_id: int,
    current: str = Form(""),
    db: Session = Depends(get_db),
):
    """Прибрати свій вигляд. Чужий (або вже видалений) — тихий no-op: смуга
    просто перемальовується, бо показувати помилку за зниклий рядок нема сенсу."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    delete_view(db, user, view_id)
    return _views_panel(request, db, user, current)


@router.get("/search", response_class=HTMLResponse)
def get_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

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
        return login_redirect(request)
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
        return login_redirect(request)
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
    plain sync button.

    Runs in a background thread (start_background_import) so the request returns
    at once — the multi-minute proxy read used to hang the tab with no feedback.
    The operator keeps working while the queue's status dot pulses «синхронізує…»
    (the 3s /sheets/state poll), and the result lands as a toast when done."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    if start_background_import():
        request.session["sync_flash"] = {
            "kind": "success",
            "message": "Імпорт історії почато. Стежте за індикатором синхронізації "
            "— результат зʼявиться, коли завершиться.",
        }
    else:
        request.session["sync_flash"] = {
            "kind": "info",
            "message": "Імпорт історії вже виконується.",
        }
    return RedirectResponse("/", status_code=303)


@router.post("/sheets/reconcile-deletions")
def reconcile_sheet_deletions(request: Request, db: Session = Depends(get_db)):
    """Підтверджена звірка масового видалення. Звичайний синк має запобіжник:
    коли за один тік «зникає» >25% рядків вкладки, він вважає це поганим
    читанням і НЕ архівує (бойовий інцидент: тік помилково зняв 17 живих
    робіт). Але обірване читання й справжнє масове видалення з одного читання
    не розрізнити, тож коли оператор СВІДОМО видалив пачку рядків у таблиці,
    ця дія обходить поріг (force_reconcile) і таки прибирає їх із черги в Архів.
    Помилка самозагоюється: рядок, що насправді лишився в таблиці, повернеться
    наступним синком через 10 хв. Admin + loopback, як і решта дій синку."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    # Знімаємо поріг ЛИШЕ для вкладок, чиї видалення запобіжник зараз тримає —
    # саме їх показує банер і саме їх підтверджує оператор. Раніше сюди йшов
    # булевий прапорець, і одна підтверджена вкладка залишала без захисту весь
    # прогін: сусідній день, прочитаний обрізано, летів в Архів (аудит 05.09.26,
    # синк H-4).
    held_tabs = set(mass_vanish_pending().keys())
    if not held_tabs:
        request.session["sync_flash"] = {
            "kind": "info",
            "message": "Немає притриманих видалень — звіряти нічого.",
        }
        return RedirectResponse("/", status_code=303)

    try:
        summary = sync_google_sheets(db, trigger="manual", force_reconcile_tabs=held_tabs)
    except SheetSyncError as exc:
        request.session["sync_flash"] = {"kind": "error", "message": str(exc)}
    else:
        request.session["sync_flash"] = {
            "kind": "success",
            "message": f"Звірку завершено: прибрано {summary.deleted} видалених рядків у Архів. "
            + summary_message(summary),
        }
    return RedirectResponse("/", status_code=303)


@router.get("/sheets/state", response_class=HTMLResponse)
def sheet_sync_state(request: Request, db: Session = Depends(get_db)):
    """Tiny self-polling fragment (_sync_indicator.html) for the queue's Google
    Sheets status dot — refreshed every few seconds so «syncing now» is visible
    live without the heavy full get_queue body running on that cadence.

    When a background import has just finished, its one-shot result is popped
    here and attached as an HX-Trigger toast, so the operator learns the outcome
    even though the work ran off-request."""
    user = get_current_user(request, db)
    if user is None:
        # Logged-out poll: swap in a NON-polling wrapper so the 3s poll STOPS
        # (a 204 makes HTMX keep the old element and keep hammering this route
        # forever). The next full navigation re-adds the live indicator.
        return HTMLResponse('<span class="tl-sync-live"></span>')

    context = {
        "user": user,
        "sync_status": live_sync_status(db),
        "sheets_configured": sheets_configured(db),
        "peeks": {"sync": queue_sync_summary(db)},
        # Кружок здоровʼя синку в рейці їде «зайцем» у цій же відповіді
        # (hx-swap-oob), бо полл сюди й так ходить кожні 3 с — окремий полл на
        # кружок подвоїв би запити на кожному екрані. Лише адміну: у оператора
        # пункт «Журнал синку» не рендериться, і htmx лаявся б у консоль на
        # ненайдену ціль OOB-свапу.
        "oob_dot": user.role == "адмін",
    }
    response = templates.TemplateResponse(request, "_sync_indicator.html", context)
    flash = pop_import_flash()
    if flash:
        response.headers["HX-Trigger"] = json.dumps(
            {"toast": {"message": flash["message"], "kind": flash["kind"]},
             "refresh-queue": True}
        )
    return response


@router.get("/sum3d/tray", response_class=HTMLResponse)
def sum3d_tray(request: Request, db: Session = Depends(get_db)):
    """Self-polling мono-лоток захоплених Sum3D ID для шапки черги (хід 1).

    Сканує теку проєктів Sum3D (Cam-work, налаштування sum3d_projects_path) і
    показує найновіші ID, щоб оператор не переписував їх вручну. Читання-лише.
    Порожній шлях/тека → порожня обгортка (полл лишається, з'явиться щойно
    вкажуть шлях і виникнуть проєкти)."""
    user = get_current_user(request, db)
    if user is None:
        # Розлогінений полл — зупинити (непорожня НЕ-полльна відповідь).
        return HTMLResponse('<span class="sum3d-tray-off"></span>')
    projects = scan_sum3d_projects(get_sum3d_projects_path(db))
    return templates.TemplateResponse(
        request, "_sum3d_tray.html", {"user": user, "sum3d_projects": projects}
    )


@router.get("/sheets/mass-vanish", response_class=HTMLResponse)
def sheet_mass_vanish_banner(request: Request, db: Session = Depends(get_db)):
    """Self-polling banner fragment (_mass_vanish_banner.html) — makes the
    «schema masove vydalennya» warning appear/disappear LIVE (every 15s) instead
    of only on a full page reload. Reads in-memory state only (mass_vanish_pending
    + the user), no DB beyond auth, so the extra poll is cheap."""
    user = get_current_user(request, db)
    if user is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "_mass_vanish_banner.html",
        {
            "user": user,
            "mass_vanish": mass_vanish_pending(),
            "header_mismatch": header_mismatch_pending(),
        },
    )


@router.get("/system/load", response_class=HTMLResponse)
def system_load(request: Request, db: Session = Depends(get_db)):
    """Жива стрічка навантаження в шапці черги — власний полл (4 с).

    Читає ЛИШЕ стан у пам'яті (фоновий семплер web.py його оновлює): у момент
    запиту нічого не міряємо, бо `cpu_percent(interval=…)` заблокував би
    обробник. Обгортку віддаємо завжди — навіть коли ще немає першого заміру
    чи psutil відсутній: інакше елемент зник би з DOM разом зі своїм поллом."""
    if get_current_user(request, db) is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    return templates.TemplateResponse(
        request, "_system_load.html", {"request": request, "load": system_load_snapshot()}
    )
