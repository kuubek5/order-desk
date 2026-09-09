"""Паспорт роботи й дії над нею: Sum3D, оператор, коментар CAM, статус,
ручне додавання, видалення, «Крок назад / вперед» і журнал дій.

Кожна зміна даних пишеться в ActionLog разом з іменем оператора — це вимога
(CLAUDE.md §10): в історії роботи завжди має бути видно, ХТО що зробив. На
цьому ж журналі тримається скасування.
"""

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from urllib.parse import urlencode

from app import sync_control
from app.business_day import business_today, utc_now
from app.models import (
    ActionLog,
    Comment,
    Order,
    StatusEvent,
    User,
)
from app.order_folder import (
    attach_export_folder_uris,
    attach_job_code_folder_uris,
)
from app.routers.deps import (
    SYNC_PAUSED_MSG,
    attach_action_toast,
    attach_sync_error_toast,
    get_current_user,
    login_redirect,
    get_db,
    templates,
    toast_response,
)
from app.services.manual_add import (
    create_manual_batch,
    normalize_target_tab,
    normalize_work_type,
)
from app.services.queue import RETENTION_DAYS, order_is_archived
from app.manual_add_flag import manual_add_in_flight
from app.services.sheet_writeback import (
    append_comment_background,
    append_manual_rows_warm,
    await_on_writeback,
    clear_sheet_row_background,
    submit_sheet_write,
    write_calculated_cell_warm,
    write_rework_sum3d_fields_warm,
    write_sheet_fields_background,
    write_sheet_fields_warm,
)
from app.services.focus import clear_all as clear_focus, focused_ids, release as release_focus, toggle as toggle_focus
from app.services.undo import (
    UNDOABLE_ACTION_TYPES,
    UNDO_WINDOW_SECONDS,
    UndoOutcome,
    log_action,
    perform_redo,
    perform_undo,
)
from app.sheet_writer import apply_status_markers
from app.services.order_path import build_path as build_order_path
from app.statuses import STATUSES, STATUS_ACCEPTED, STATUS_NEW

logger = logging.getLogger(__name__)

router = APIRouter()


def _row_context(request: Request, db: Session, order, sync_error) -> dict:
    """Контекст для повторного рендера ОДНОГО рядка черги.

    Існує рівно заради `focused_ids`: мітка «мої зараз» персональна, тож рядок
    може її намалювати лише знаючи, хто дивиться. Кожен роут, що віддає
    _order_row.html, мусить іти через цей хелпер — інакше після будь-якої дії
    (Sum3D, оператор, коментар) рядок повернувся б без чужої… власної мітки, і
    це читалось би як «система забула», а не як пропущений ключ контексту.
    Сторож: tests/test_order_focus.py::test_every_row_render_passes_focused_ids.
    """
    user = get_current_user(request, db)
    return {
        "order": order,
        "statuses": STATUSES,
        "sync_error": sync_error,
        "focused_ids": focused_ids(db, user),
    }


@router.post("/orders/{order_id}/sum3d-id", response_class=HTMLResponse)
async def set_sum3d_id(
    request: Request,
    order_id: int,
    # Default "" (not Form(...)): an EMPTY value is a valid, meaningful input —
    # the operator deleting a mistakenly-entered Sum3D to send the work back to
    # «можна брати». Form(...) treated an empty form field as missing and 422'd,
    # so clearing was impossible from the UI at all.
    sum3d_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            _row_context(request, db, order, SYNC_PAUSED_MSG),
        )

    value = sum3d_id.strip() or None
    # Entering a Sum3D ID IS the "I calculated this in Sum3D" moment, so the
    # portal stamps the operator's letter into the "Прорахував" column — М for a
    # normal work, Х for a rework — matching the lab's existing by-hand
    # convention. Only when the operator actually has a letter assigned and a
    # value is being set (never on a clear); an operator without a letter just
    # gets the Sum3D written, "Прорахував" left as-is.
    initial = (user.sheet_initial or "").strip() or None
    stamp = initial if (initial and value) else None
    rework = order.active_rework
    write_fields: set[str] = set()
    # Full before-snapshot so "Скасувати" reverts EVERYTHING this action touched
    # (Sum3D + the auto-stamped letter + the auto-advanced status), not just the
    # Sum3D cell — a real "крок назад".
    if rework is not None:
        before = {"rework.sum3d_id": rework.sum3d_id, "rework.calculated_raw": rework.calculated_raw}
    else:
        before = {"sum3d_id": order.sum3d_id, "calculated_raw": order.calculated_raw, "status": order.status}

    if rework is not None:
        # A reworked job — the ID the operator types is the redo calculation's
        # Sum3D (column W), NOT the original job's ID (column L, left intact as
        # the "previous calculation" the operator reviews to avoid repeating the
        # mistake — see the order passport's rework block). The letter goes to
        # the rework "Прорахував" (column Х), the redo counterpart of М.
        rework.sum3d_id = value
        if stamp:
            rework.calculated_raw = stamp
        after = {"rework.sum3d_id": rework.sum3d_id, "rework.calculated_raw": rework.calculated_raw}
        note = f"Sum3D переробки → {value}" if value else "Sum3D переробки очищено"
        undo_field = "rework.sum3d_id"
    else:
        order.sum3d_id = value
        write_fields = {"sum3d_id"}
        if stamp:
            order.calculated_raw = stamp
            write_fields.add("calculated_raw")
            # The letter in М is the "прораховано" marker, so advance the DB
            # status to match (never downgrade a further state), recording the
            # real logged-in operator who calculated it.
            if order.status in (STATUS_NEW, STATUS_ACCEPTED):
                order.status = "прораховано"
                db.add(StatusEvent(
                    order_id=order.id, operator_id=user.id,
                    status="прораховано", actor=user.username,
                ))
        after = {"sum3d_id": order.sum3d_id, "calculated_raw": order.calculated_raw, "status": order.status}
        note = f"Sum3D → {value}" if value else "Sum3D очищено"
        undo_field = "sum3d_id"

    log_entry = log_action(
        db, order=order, operator=user, action_type="sum3d", field=undo_field,
        old=json.dumps(before, ensure_ascii=False),
        new=json.dumps(after, ensure_ascii=False), note=note,
    )
    # Мітка «беру зараз» існує рівно для того, щоб не загубити, КУДИ вписувати
    # Sum3D. Вписали — причина відпала, мітка знімається в тій самій транзакції.
    # Набір самоочищується, і «Зняти всі» лишається рідкісною ручною дією.
    # Знімається лише МОЯ мітка: якщо роботу тримає в наборі й колега, це його
    # набір, і чистити його не наша справа. При очищенні ID (value порожнє)
    # мітку не чіпаємо — робота знову «в руках».
    if value:
        release_focus(db, order, user)
    db.commit()

    # Таблиця — ПІСЛЯ коміту і НЕ на event loop: воркер write-back читає вже
    # збережені значення власною сесією, а `await` тримає застосунок живим,
    # поки Google відповідає (аудит 05.09.26, синк C-2).
    if rework is not None:
        sync_error = await await_on_writeback(
            write_rework_sum3d_fields_warm, order.id, value or "", stamp
        )
    else:
        sync_error = await await_on_writeback(write_sheet_fields_warm, order.id, write_fields)
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    response = templates.TemplateResponse(
        request, "_order_row.html", _row_context(request, db, order, sync_error)
    )
    if sync_error is None:
        attach_action_toast(response, log_entry, note)
    else:
        attach_sync_error_toast(response, note, sync_error)
    return response


@router.post("/orders/{order_id}/operator", response_class=HTMLResponse)
async def set_operator(request: Request, order_id: int, operator: str = Form(""), db: Session = Depends(get_db)):
    """Set OR clear the «Прорахував» cell (column М → Order.calculated_raw): who
    calculated the work. The operator types the letter/code straight in the queue
    row and it writes back to the sheet; an empty value clears it (the ✕ button
    just empties the input). Logged as an undoable "operator" action. Does not
    touch Sum3D or readiness (which key off job_code + sum3d_id, not this cell).

    Default "" (not Form(...)): an empty value is a valid input — clearing the
    cell — exactly like set_sum3d_id, where Form(...) would 422 a blank field."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            _row_context(request, db, order, SYNC_PAUSED_MSG),
        )

    value = operator.strip()
    old_value = order.calculated_raw
    if (old_value or "") == value:
        # No change — return the row untouched, no log, no toast.
        attach_export_folder_uris(db, [order])
        attach_job_code_folder_uris(db, [order])
        return templates.TemplateResponse(
            request, "_order_row.html", _row_context(request, db, order, None)
        )

    order.calculated_raw = value
    note = f"оператор → {value}" if value else "оператора очищено"
    log_entry = log_action(
        db, order=order, operator=user, action_type="operator", field="calculated_raw",
        old=old_value or "", new=value, note=note,
    )
    db.commit()
    # Запис у таблицю — на воркері write-back, не на event loop (синк C-2).
    sync_error = await await_on_writeback(write_calculated_cell_warm, order.id, value)
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    response = templates.TemplateResponse(
        request, "_order_row.html", _row_context(request, db, order, sync_error)
    )
    if sync_error is None:
        attach_action_toast(response, log_entry, note)
    else:
        attach_sync_error_toast(response, note, sync_error)
    return response


@router.post("/orders/{order_id}/cam-comment", response_class=HTMLResponse)
async def set_cam_comment(
    request: Request,
    order_id: int,
    cam_comment: str = Form(""),
    db: Session = Depends(get_db),
):
    """Inline edit of the CAM comment straight from the queue row. Unlike the
    passport's /comments (which appends to the two-way history preserving other
    people's sheet edits), this SETS the CAM-comment cell to exactly what the
    operator typed — the "enter/edit my own note in the row" flow. Writes back
    to the sheet's comment column for lab orders, same discipline as sum3d-id."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            _row_context(request, db, order, SYNC_PAUSED_MSG),
        )

    old_comment = order.cam_comment
    order.cam_comment = cam_comment.strip() or None
    if (old_comment or "") != (order.cam_comment or ""):
        log_action(
            db, order=order, operator=user, action_type="cam_comment", field="cam_comment",
            old=old_comment or "", new=order.cam_comment or "",
            note=(f"коментар → {order.cam_comment}" if order.cam_comment else "коментар очищено"),
        )
    db.commit()  # persist immediately — the row must feel instantly saved
    db.refresh(order)

    # Mirror to the sheet in the background so a slow Google write never stalls
    # the inline edit (see write_sheet_fields_background).
    write_sheet_fields_background(order.id, {"cam_comment"})

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    return templates.TemplateResponse(
        request, "_order_row.html", _row_context(request, db, order, None)
    )


@router.get("/orders/new", response_class=HTMLResponse)
def new_order_form(
    request: Request, db: Session = Depends(get_db), error: str = "",
    work_type: str = "client", material_color: str = "", client_name: str = "",
    work_order_no: str = "", kind: str = "", quantity: str = "", sum3d_id: str = "",
    job_code: str = "", technician_name: str = "",
    return_to: str = "", target_tab: str = "",
):
    """Окремої сторінки «додати роботу» більше немає — форма живе в Черзі.

    Це була друга копія тієї самої форми (аудит 05.09.26, крок 2.4): у Черзі
    вона вже є inline під кнопкою «+», і саме нею користуються. Окрема сторінка
    лишалась дорогою для помилок: невдала валідація викидала оператора з черги
    на порожній екран, а повернувшись, він бачив чергу вже без свого дня й
    фільтрів. Тепер будь-який шлях сюди веде назад у чергу з розгорнутою
    формою; помилка показується в ній самій.

    Роут лишається (а не видаляється) навмисно: на нього ще ведуть закладки й
    старі посилання, і множина роутів у знімку `tests/route_inventory.txt` не
    змінюється.
    """
    if get_current_user(request, db) is None:
        return login_redirect(request)

    # `return_to` — куди оператор просив повернутись; він і є цільовою чергою.
    # Порожній або чужий (зовнішній хост) — просто корінь.
    target = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/"
    params = {"add": "1"}
    if error:
        params["add_error"] = error
    if work_type in ("client", "lab"):
        params["add_type"] = work_type
    if target_tab:
        params["target_tab"] = target_tab
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}{urlencode(params)}", status_code=303)


@router.post("/orders/new")
def create_manual_order(
    request: Request,
    work_type: str = Form("client"),
    return_to: str = Form(""),
    target_tab: str = Form(""),
    client_name: list[str] = Form([]),
    work_order_no: list[str] = Form([]),
    kind: list[str] = Form([]),
    material_color: list[str] = Form([]),
    quantity: list[str] = Form([]),
    sum3d_id: list[str] = Form([]),
    job_code: list[str] = Form([]),
    technician_name: list[str] = Form([]),
    opak: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    """Форма ручного додавання: розібрати, віддати сервісу, повернути в чергу.

    Самі правила партії — кілька робіт одним пушем, види «клієнт»/«лабораторія»,
    вибір вкладки, захист від подвійного сабміту, запис у таблицю й народження
    Order-ів — живуть у `app/services/manual_add.py`. Тут лишається суто HTTP:
    куди повернути оператора, гейт паузи синку та очікування результату запису.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    work_type = normalize_work_type(work_type)
    is_lab = work_type == "lab"

    # Send the operator back to the exact queue view they submitted from — same
    # day tab, same filters — instead of resetting them to the default queue.
    # Only a same-origin relative path is accepted: an absolute or
    # protocol-relative value would turn this form into an open redirect.
    default_target = f"/?source={'lab' if is_lab else 'client'}"
    target = return_to.strip() if isinstance(return_to, str) else ""
    if not target.startswith("/") or target.startswith("//"):
        target = default_target

    # Вкладку з екрана звіряємо ще ДО валідації рядків: невдала спроба має
    # повернути оператора на ТОЙ САМИЙ день, а не скинути його на типовий.
    wanted_tab = normalize_target_tab(target_tab)

    def _back(message: str):
        """Помилка — назад У ЧЕРГУ, з розгорнутою формою і текстом у ній.

        Раніше вело на окрему сторінку `/orders/new`, і оператор втрачав день,
        фільтри та прокрутку через невдалу валідацію одного поля (аудит
        05.09.26, крок 2.4). `target` — та сама черга, з якої він тиснув.
        """
        params = {"add": "1", "add_error": message}
        if work_type in ("client", "lab"):
            params["add_type"] = work_type
        if wanted_tab:
            params["target_tab"] = wanted_tab
        separator = "&" if "?" in target else "?"
        return RedirectResponse(f"{target}{separator}{urlencode(params)}", status_code=303)

    if sync_control.is_paused():
        return _back(SYNC_PAUSED_MSG)

    def _write_rows(day, works, *, paint_blue, placement, target_tab):
        """Дописати партію в таблицю й дочекатись відповіді.

        Append the whole batch on the warm write-back worker (cached spreadsheet/
        worksheet) in ONE sheet call — the cold request thread would re-pay ~40s of
        open+worksheet through the lab proxy per call. The worker resolves the
        newest dated tab ≤ today (today's tab often isn't created yet) and returns
        which tab it actually wrote to, so the orders land on the same day.

        Через спільну точку пулу: там же живе остання перевірка паузи, щоб
        жоден запис не проліз повз неї (аудит 05.09.26, синк M-8). Очікування
        результату лишається тут, у HTTP-шарі: сервіс не має знати ні про пул,
        ні про таймаути запиту.
        """
        return submit_sheet_write(
            append_manual_rows_warm, day, works,
            paint_blue=paint_blue, placement=placement, target_tab=target_tab,
        ).result(timeout=120)

    # Позначка «зараз додаю» на ОБИДВА кроки: запис у таблицю і коміт у базу.
    # Між ними є щілина, і фоновий синк, влучивши в неї, створює свою копію тієї
    # самої роботи — коронку фрезерують двічі. Позначка сама протухає, тож
    # забути її зняти не страшно (app/manual_add_flag.py, аудит 08.09.26).
    with manual_add_in_flight():
        result = create_manual_batch(
            db, user=user, work_type=work_type, target_tab=wanted_tab,
            client_name=client_name, work_order_no=work_order_no, kind=kind,
            material_color=material_color, quantity=quantity, sum3d_id=sum3d_id,
            job_code=job_code, technician_name=technician_name, opak=opak,
            write_rows=_write_rows,
        )
    if result.error is not None:
        return _back(result.error)

    # Повтор сабміту (F5) виглядає для оператора так само, як успіх: у таблицю
    # й БД не пішло нічого, але «помилки» не сталось.
    return RedirectResponse(target, status_code=303)


@router.post("/orders/{order_id}/focus", response_class=HTMLResponse)
def toggle_order_focus(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Поставити/зняти особисту мітку «беру зараз».

    Відповідь — сам рядок: свап одного <tr> дає миттєвий відгук на самій
    шпильці. Слідом летить `refresh-queue`, бо пришпилені роботи спливають
    нагору (див. сортування в routers/queue.py): без нього рядок переїжджав
    би аж на наступному 15-секундному поллі, тобто до пів хвилини «нічого не
    сталось» після кліку.

    Переїзд — це відповідь на СВІДОМЕ клацання оператора, а не самовільний
    рух списку, тому писане правило стабільного порядку (CLAUDE.md §2) не
    зачіпається: фоновий полл порядку не міняє.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="роботу не знайдено")

    toggle_focus(db, order, user)
    db.commit()

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])
    response = templates.TemplateResponse(
        request, "_order_row.html", _row_context(request, db, order, None)
    )
    response.headers["HX-Trigger-After-Swap"] = json.dumps({"refresh-queue": True})
    return response


@router.post("/orders/focus/clear")
def clear_order_focus(request: Request, db: Session = Depends(get_db)):
    """Зняти всі свої мітки. Підтвердження питає інтерфейс (hx-confirm):
    набір із двох десятків рядків збирається руками, і випадковий клік
    коштував би заходу."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    removed = clear_focus(db, user)
    db.commit()
    if not removed:
        return toast_response("Набір і так порожній.", kind="info")
    return toast_response(f"Знято позначок: {removed}.", triggers={"refresh-queue": True})


@router.post("/orders/{order_id}/change-seen", response_class=HTMLResponse)
async def dismiss_sheet_change(
    request: Request, order_id: int, db: Session = Depends(get_db)
):
    """Clear the "technician corrected this row" mark once the operator has
    looked at it.

    Dismissal is theirs alone — no timer (user decision 25.08.26). The mark
    exists to stop someone milling a version of the work that has since been
    corrected, and a change that expires on its own can expire during a break,
    which is exactly when it would have been missed. Re-renders the row so the
    badge disappears without reloading the queue."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="роботу не знайдено")

    if order.sheet_changed_at is not None:
        db.add(
            StatusEvent(
                order_id=order.id, operator_id=user.id, status=order.status,
                actor=user.username,
                note=f"переглянув зміни техніка: {order.sheet_changed_fields or '—'}",
            )
        )
        order.sheet_changed_at = None
        order.sheet_changed_fields = None
        db.commit()

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])
    return templates.TemplateResponse(
        request, "_order_row.html",
        _row_context(request, db, order, None),
    )


@router.post("/orders/{order_id}/delete")
async def delete_order(
    request: Request,
    order_id: int,
    inline: str = Form(""),
    db: Session = Depends(get_db),
):
    """Remove a work from the queue and blank its row in the sheet.

    Archive, don't destroy: the order keeps its history and moves to «Архів»,
    matching what a row vanishing from the sheet already does (see
    app/sync.py). Deleting the DB row instead would drop its StatusEvents and
    comments, and the next sync would happily re-import the work anyway.

    The sheet row is BLANKED on the background writer, so the operator isn't
    held for a Google round-trip through the lab proxy. Email-sourced works
    (source="email") never had a sheet row — for them this is DB-only.

    Two callers, two replies. From the work card (``inline`` unset) the page
    the operator is looking at no longer exists, so we send them to the queue.
    From a queue row (``inline``) we reply with the plain toast and no redirect:
    the operator keeps the day tab, filters and scroll they were working in, and
    the row is removed client-side (see _order_row.html). Deliberately NOT a
    body swapped over the row — HTMX fires HX-Trigger on the requesting element,
    so swapping the row away first detaches the form and the toast event never
    bubbles to the listener on <body>. The row would vanish silently.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="роботу не знайдено")
    if order.archived_at is not None:
        return toast_response("Робота вже в архіві", kind="info")

    # Delete blanks the sheet row, so it counts as a table write — refused while
    # paused (deletion also archives the order, which we must not do half-way).
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    order.archived_at = utc_now()
    db.add(
        StatusEvent(
            order_id=order.id, operator_id=user.id, status=order.status,
            actor=user.username, note="видалено з черги",
        )
    )
    # Logged so the delete is both visible in the journal and reversible: undo
    # un-archives the work and re-fills the sheet row it blanked. The most
    # valuable undo of all — a mis-clicked delete is otherwise retyped by hand.
    log_action(
        db, order=order, operator=user, action_type="delete", field="archived_at",
        old="", new="archived",
        note=f"видалено з черги: {order.work_order_no or order.client_name or ('#' + str(order.id))}",
    )
    db.commit()

    # Про скасування кажемо В МОМЕНТ видалення. Воно працювало й раніше
    # («Крок назад» повертає роботу й рядок у таблиці), але про це не знав
    # ніхто: тост повідомляв лише про видалення, а стрілка в шапці виглядає як
    # навігація «назад». Кнопку в тост не повертаємо — її свідомо прибрали на
    # користь двох статичних стрілок; називаємо саме їх (прохання власника,
    # 07.09.26).
    if order.source in ("lab", "sheet_client") and order.sheet_tab and order.row_number:
        clear_sheet_row_background(order.id)
        message = "Роботу видалено з черги, рядок у таблиці очищено · «Крок назад» (←) поверне"
    else:
        message = "Роботу видалено з черги · «Крок назад» (←) поверне"

    if request.headers.get("HX-Request") == "true":
        if isinstance(inline, str) and inline.strip():
            return toast_response(message)
        response = toast_response(message)
        # Хай сторінка перемалюється — рядок має зникнути з черги одразу.
        response.headers["HX-Redirect"] = "/"
        return response
    return RedirectResponse("/", status_code=303)


@router.post("/orders/{order_id}/status", response_class=HTMLResponse)
async def set_status(
    request: Request,
    order_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    if status not in STATUSES:
        raise HTTPException(status_code=400, detail="невідомий статус")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    if sync_control.is_paused():
        return templates.TemplateResponse(
            request, "_order_row.html",
            _row_context(request, db, order, SYNC_PAUSED_MSG),
        )

    old_status = order.status
    order.status = status
    sheet_fields = apply_status_markers(
        order,
        status,
        actor=user.full_name or user.username,
    )
    db.add(
        StatusEvent(order_id=order.id, operator_id=user.id, status=status, actor=user.username)
    )
    log_entry = None
    if status != old_status:
        log_entry = log_action(
            db, order=order, operator=user, action_type="status",
            field="status", old=old_status, new=status,
            note=f"статус: {old_status} → {status}",
        )
    db.commit()
    # Запис у таблицю — на воркері write-back, не на event loop (синк C-2).
    sync_error = await await_on_writeback(write_sheet_fields_warm, order.id, sheet_fields)
    db.refresh(order)

    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    response = templates.TemplateResponse(
        request, "_order_row.html", _row_context(request, db, order, sync_error)
    )
    if sync_error is not None:
        attach_sync_error_toast(response, f"статус → {status}", sync_error)
    elif log_entry is not None:
        attach_action_toast(response, log_entry, f"статус → {status}")
    return response


# Action types a "крок назад" can revert, newest-first selection order.
def undo_response(outcome: UndoOutcome) -> Response:
    """Один переклад результату дії у відповідь HTMX. `refresh_queue` стає
    HX-Trigger `refresh-queue`, щоб опитувана черга показала змінений рядок
    одразу, а не через 15с."""
    triggers = {"refresh-queue": True} if outcome.refresh_queue else None
    return toast_response(outcome.message, kind=outcome.kind, triggers=triggers)


def _perform_undo(db: Session, user: "User", entry: ActionLog) -> Response:
    return undo_response(perform_undo(db, user, entry))


@router.post("/actions/{action_id}/undo")
def undo_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    """Undo one specific logged action by id (guards: own action, once, within
    the window, sync not paused), then revert it via _perform_undo.

    Синхронний `def` НАВМИСНО: скасування пише в таблицю (`perform_undo` →
    write_sheet_fields / restore_sheet_row), а FastAPI виконує `async def`
    прямо на event loop — тобто холодне відкриття таблиці заморожувало б увесь
    застосунок. Звичайний `def` FastAPI віддає в threadpool (аудит 05.09.26,
    синк C-2)."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    entry = db.get(ActionLog, action_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="дію не знайдено")
    if entry.operator_id != user.id:
        return toast_response("Скасувати можна лише власну дію", kind="error")
    if entry.undone_at is not None:
        return toast_response("Цю дію вже скасовано", kind="error")
    if entry.created_at is not None and entry.created_at < datetime.now() - timedelta(seconds=UNDO_WINDOW_SECONDS):
        return toast_response("Вікно скасування минуло", kind="error")
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    return _perform_undo(db, user, entry)


@router.post("/actions/undo-last")
def undo_last_action(request: Request, db: Session = Depends(get_db)):
    """«Крок назад» — the static undo button. Синхронний `def` з тієї ж причини,
    що й `undo_action`: запис у таблицю не має жити на event loop. Finds THIS operator's most recent
    still-undoable action (any of UNDOABLE_ACTION_TYPES — Sum3D, status, operator,
    CAM comment, delete — not yet undone, inside the window) and reverts it.
    Pressing it again steps back through earlier actions. Replaces the per-action
    «Скасувати» toast affordance."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    cutoff = datetime.now() - timedelta(seconds=UNDO_WINDOW_SECONDS)
    entry = (
        db.query(ActionLog)
        .filter(
            ActionLog.operator_id == user.id,
            ActionLog.action_type.in_(UNDOABLE_ACTION_TYPES),
            ActionLog.undone_at.is_(None),
            ActionLog.created_at >= cutoff,
        )
        .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
        .first()
    )
    if entry is None:
        return toast_response("Немає що скасувати", kind="info")

    return _perform_undo(db, user, entry)


def _perform_redo(db: Session, user: "User", entry: ActionLog) -> Response:
    return undo_response(perform_redo(db, user, entry))


@router.post("/actions/redo-last")
# Звичайний `def`, як і undo_action/undo_last_action: perform_redo пише в
# таблицю СИНХРОННО (gspread), тож роут мусить іти в threadpool, а не на event
# loop (CLAUDE.md §14 «Запис у таблицю»). Код-ревʼю 07.09.26.
def redo_last_action(request: Request, db: Session = Depends(get_db)):
    """«Крок вперед» — the static redo button. Re-applies THIS operator's most
    recently undone action (any of UNDOABLE_ACTION_TYPES) that is still inside the
    window. Pressing it again steps forward through earlier undos, mirroring
    «Крок назад»."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    cutoff = datetime.now() - timedelta(seconds=UNDO_WINDOW_SECONDS)
    entry = (
        db.query(ActionLog)
        .filter(
            ActionLog.operator_id == user.id,
            ActionLog.action_type.in_(UNDOABLE_ACTION_TYPES),
            ActionLog.undone_at.is_not(None),
            ActionLog.undone_at >= cutoff,
        )
        .order_by(ActionLog.undone_at.desc(), ActionLog.id.desc())
        .first()
    )
    if entry is None:
        return toast_response("Немає що повторити", kind="info")

    return _perform_redo(db, user, entry)


# How many entries the «Останні дії» popup shows. Deliberately short: this is a
# "where was I just now" locator, not the audit log — /journal owns depth.
RECENT_ACTIONS_LIMIT = 10

# What the popup LISTS — a superset of what ← → can revert. Creating a work by
# hand is a real place the operator was and must be jumpable, but reverting it
# would mean deleting a row from the shared sheet, which stays behind the
# explicit delete button (with its confirm) rather than a one-click arrow.
# Excluded from both: the "undo"/"redo" bookkeeping rows, which would otherwise
# bury the actual edits every time the operator steps back.
RECENT_ACTION_TYPES = UNDOABLE_ACTION_TYPES + ("create",)


@router.get("/actions/recent", response_class=HTMLResponse)
def get_recent_actions(
    request: Request,
    tab: str = "",
    db: Session = Depends(get_db),
):
    """Render the «Останні дії» popup — this operator's last data-changing actions,
    newest first, each one clickable to jump to the work it touched.

    Scoped to the current operator on purpose: it reads the same ActionLog rows
    «Крок назад» steps through, so what the list shows and what the arrows can
    revert never disagree. Clicking an entry NEVER changes data — it only locates
    the row (see app.js) — so a stray click at the bench is harmless.

    ``tab`` is the day tab the queue is showing, used only to badge entries whose
    work lives on a different day (the jump then navigates instead of scrolling).
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    entries = db.execute(
        select(ActionLog)
        .options(selectinload(ActionLog.order))
        .where(
            ActionLog.operator_id == user.id,
            ActionLog.action_type.in_(RECENT_ACTION_TYPES),
        )
        .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
        .limit(RECENT_ACTIONS_LIMIT)
    ).scalars().all()

    return templates.TemplateResponse(
        request, "_actions_recent.html", {"entries": entries, "viewed_tab": tab.strip()}
    )


@router.post("/orders/{order_id}/comments")
async def add_order_comment(
    request: Request,
    order_id: int,
    text: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    clean_text = text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="коментар не може бути порожнім")

    # Коментар дописується в живу клітинку K таблиці, тобто це ЗАПИС — а цей
    # роут єдиний із чотирнадцяти не питав про паузу. Адмін ставив паузу, щоб
    # руками перебудувати вкладку, і саме туди летів коментар (аудит 05.09.26,
    # синк M-8). Пул тепер має власну страховку, але операторові потрібна
    # зрозуміла відповідь, а не мовчазний пропуск.
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    now = datetime.now()
    author = user.full_name or user.username
    comment = Comment(
        order_id=order.id,
        source="portal",
        author=author,
        text=clean_text,
    )
    db.add(comment)
    db.commit()

    # Запис коментаря в таблицю — best-effort і у ФОНІ: раніше це відкривало
    # Google прямо в потоці запиту й додавання зависало на час відповіді
    # (до ~40с холодним на лаб-проксі). Коментар у базі вже збережено; таблиця
    # наздоганяє, помилка йде в SyncLog.
    if order.sheet_tab and order.source == "lab":
        line = f"[{now:%d.%m.%Y %H:%M} · {author}] {clean_text}"
        append_comment_background(order.id, comment.id, line)

    return RedirectResponse(f"/orders/{order.id}", status_code=303)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def get_order_detail(
    request: Request,
    order_id: int,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    # Archived work (out of the retention window or removed from Google) is a
    # historical record — the passport opens read-only so the operator reviews
    # it (Sum3D, timeline) without editing frozen history.
    # Межа — від РОБОЧОЇ доби, як у черзі (queue.py) і в архіві: з `date.today()`
    # вікно паспорта на добу відставало від вікна черги щоночі, і робота, яку
    # черга показує живою, відкривалась замороженою «тільки для читання» —
    # оператор нічної зміни не міг вписати в неї Sum3D.
    read_only = order_is_archived(
        order, business_today() - timedelta(days=RETENTION_DAYS)
    )

    # Тека роботи й STL-токен для правої панелі паспорта. Досі паспорт читав
    # `order.export_folder_uri`, якого тут ніхто не виставляв (це transient
    # з attach_*, а не колонка), тож іконка теки в паспорті не рендерилась
    # НІКОЛИ — Jinja мовчки бачила Undefined. Обидва корені, як в архіві
    # (_with_folders): пошта живе в export, лабораторія — у теці техніків.
    attach_export_folder_uris(db, [order])
    attach_job_code_folder_uris(db, [order])

    # Laconic action journal for THIS work (Sum3D/status/undo), newest first —
    # one line per operator action, the "хто що зробив" record.
    actions = db.execute(
        select(ActionLog)
        .where(ActionLog.order_id == order.id)
        .order_by(ActionLog.created_at.desc())
    ).scalars().all()

    return templates.TemplateResponse(
        request,
        "order_detail.html",
        {
            "order": order,
            "user": user,
            "error": error,
            "statuses": STATUSES,
            "read_only": read_only,
            "actions": actions,
            # Одна стрічка замість двох: поява роботи + статуси + дії, за часом
            # (app/services/order_path.py). Питання «коли прорахували і хто»
            # раніше вимагало зіставляти два списки очима.
            "order_path": build_order_path(order, list(actions)),
        },
    )


@router.get("/journal", response_class=HTMLResponse)
def get_journal(
    request: Request,
    operator: str = "",
    day: str = "",
    db: Session = Depends(get_db),
):
    """Лаконічний журнал дій оператора — усі змінні дії (Sum3D, статус, undo)
    стрічкою, з фільтром за оператором і днем. Прозорий для всієї команди
    (хто що зробив); показує останні дії, обмежені вікном, щоб не тягнути все."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    query = (
        select(ActionLog)
        .options(selectinload(ActionLog.order), selectinload(ActionLog.operator))
        .order_by(ActionLog.created_at.desc())
    )
    op_id = int(operator) if operator.isdigit() else None
    if op_id is not None:
        query = query.where(ActionLog.operator_id == op_id)

    day_value = ""
    if day:
        try:
            picked = datetime.strptime(day, "%Y-%m-%d")
            query = query.where(
                ActionLog.created_at >= picked,
                ActionLog.created_at < picked + timedelta(days=1),
            )
            day_value = day
        except ValueError:
            pass

    limit = 500
    entries = db.execute(query.limit(limit + 1)).scalars().all()
    truncated = len(entries) > limit
    entries = entries[:limit]

    operators = db.scalars(select(User).order_by(User.full_name, User.username)).all()

    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "user": user,
            "entries": entries,
            "operators": operators,
            "selected_operator": op_id,
            "selected_day": day_value,
            "truncated": truncated,
            "limit": limit,
        },
    )
