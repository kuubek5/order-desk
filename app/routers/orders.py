"""Паспорт роботи й дії над нею: Sum3D, оператор, коментар CAM, статус,
ручне додавання, видалення, «Крок назад / вперед» і журнал дій.

Кожна зміна даних пишеться в ActionLog разом з іменем оператора — це вимога
(CLAUDE.md §10): в історії роботи завжди має бути видно, ХТО що зробив. На
цьому ж журналі тримається скасування.
"""

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.requests import Request

from time import monotonic
from urllib.parse import urlencode

from app import sync_control
from app.models import (
    ActionLog,
    Comment,
    EmailMessage,
    Order,
    ReworkRecord,
    StatusEvent,
    SyncLog,
    User,
)
from app.order_folder import (
    attach_email_preview_tokens,
    attach_export_folder_uris,
    attach_job_code_folder_uris,
)
from app.parser import HEADER_ROWS
from app.routers.deps import (
    SYNC_PAUSED_MSG,
    attach_action_toast,
    attach_sync_error_toast,
    get_current_user,
    get_db,
    templates,
    toast_response,
)
from app.services.config_state import mail_preview_roots, sheets_configured
from app.services.operators import normalize_initial
from app.services.order_dates import order_date, parse_sheet_tab
from app.services.queue import RETENTION_DAYS, order_is_archived
from app.services.sheet_writeback import (
    append_comment_background,
    append_manual_rows_warm,
    clear_sheet_row_background,
    sheet_writeback_pool,
    write_calculated_cell,
    write_rework_sum3d_fields,
    write_sheet_fields,
    write_sheet_fields_background,
)
from app.services.focus import clear_all as clear_focus, focused_ids, release as release_focus, toggle as toggle_focus
from app.services.undo import (
    UNDOABLE_ACTION_TYPES,
    UNDO_WINDOW_SECONDS,
    UndoOutcome,
    log_action,
    perform_redo,
    perform_undo,
    snapshot_target,
)
from app.material_catalog import (
    ensure_seeded,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.sheet_writer import apply_status_markers
from app.statuses import STATUSES

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
        sync_error = write_rework_sum3d_fields(db, order, value or "", letter=stamp)
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
            if order.status in ("нове", "прийнято"):
                order.status = "прораховано"
                db.add(StatusEvent(
                    order_id=order.id, operator_id=user.id,
                    status="прораховано", actor=user.username,
                ))
        sync_error = write_sheet_fields(db, order, write_fields)
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
    sync_error = write_calculated_cell(db, order, value)
    note = f"оператор → {value}" if value else "оператора очищено"
    log_entry = log_action(
        db, order=order, operator=user, action_type="operator", field="calculated_raw",
        old=old_value or "", new=value, note=note,
    )
    db.commit()
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
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "new_order.html",
        {
            "user": user,
            "today": date.today().strftime("%d.%m.%y"),
            "error": error or None,
            # Carried through a failed validation so the retry still writes to
            # the day tab the operator started from and lands back there. This
            # page is reached almost only via _back, so losing them here meant
            # the second attempt silently fell back to today's tab.
            "return_to": return_to,
            "target_tab": target_tab,
            "form": {
                "work_type": work_type if work_type in ("client", "lab") else "client",
                "client_name": client_name, "work_order_no": work_order_no,
                "kind": kind, "material_color": material_color,
                "quantity": quantity, "sum3d_id": sum3d_id,
                "job_code": job_code, "technician_name": technician_name,
            },
        },
    )


_MAX_MANUAL_ROWS = 30

# Server-side double-submit guard for manual adds. The submit button disables
# itself client-side, but an F5 re-POST, back-button resubmit, or a browser
# retry still reaches the server and would append the same rows to the sheet
# AGAIN. Keyed by user id; an identical payload within the window is treated
# as the same submit and answered with the normal redirect, writing nothing.
# In-process state is enough: the app runs as a single local process.
_MANUAL_ADD_DEDUP_SECONDS = 30.0
_recent_manual_adds: dict[int, tuple[str, float]] = {}


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
    db: Session = Depends(get_db),
):
    """Add one OR several works by hand and mirror them into today's sheet tab.

    Each field arrives as a parallel list (one entry per form row), so a single
    submit can add several clients or several lab наряди at once. Two kinds:

      * client (default) — наряд-less client rows (client name in "Вид роботи",
        painted the lab's pending blue), source="sheet_client".
      * lab — normal internal works: наряд in "Номер наряду", вид in "Вид
        роботи", not painted, source="lab".

    The whole batch is written as one contiguous block in a single sheet call.
    Each row is linked by row_number so the next sync updates it in place."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)

    work_type = work_type if work_type in ("client", "lab") else "client"
    is_lab = work_type == "lab"

    # Send the operator back to the exact queue view they submitted from — same
    # day tab, same filters — instead of resetting them to the default queue.
    # Only a same-origin relative path is accepted: an absolute or
    # protocol-relative value would turn this form into an open redirect.
    default_target = f"/?source={'lab' if is_lab else 'client'}"
    target = return_to.strip() if isinstance(return_to, str) else ""
    if not target.startswith("/") or target.startswith("//"):
        target = default_target

    # The day tab on screen wins over "today" — see append_manual_rows_warm.
    # Validated as a real dd.mm.yy here so a hand-crafted value can only ever
    # miss and fall back, never reach the sheet layer as junk. Resolved before
    # _back so a failed validation can carry it into the retry.
    wanted_tab = target_tab.strip() if isinstance(target_tab, str) else ""
    if wanted_tab and parse_sheet_tab(wanted_tab) is None:
        wanted_tab = ""

    def _back(message: str):
        params = urlencode({
            "error": message, "work_type": work_type,
            "return_to": target, "target_tab": wanted_tab,
        })
        return RedirectResponse(f"/orders/new?{params}", status_code=303)

    def _at(values: list[str], i: int) -> str:
        return values[i].strip() if i < len(values) else ""

    if sync_control.is_paused():
        return _back(SYNC_PAUSED_MSG)

    row_count = max(
        len(client_name), len(work_order_no), len(kind), len(material_color),
        len(quantity), len(sum3d_id), len(job_code), len(technician_name),
    )
    if row_count == 0:
        return _back("Додайте хоча б одну роботу.")
    if row_count > _MAX_MANUAL_ROWS:
        return _back(f"Забагато рядків за раз (макс. {_MAX_MANUAL_ROWS}).")

    # Build the per-row work list, validating each non-empty row. Fully blank
    # rows (an extra field-set the operator left empty) are silently skipped.
    works: list[dict] = []
    for i in range(row_count):
        row_client = _at(client_name, i)
        row_naryad = _at(work_order_no, i)
        row_kind = _at(kind, i)
        row_material = _at(material_color, i)
        row_qty = _at(quantity, i)
        row_sum3d = _at(sum3d_id, i)
        row_job = _at(job_code, i)
        row_tech = _at(technician_name, i)

        if is_lab:
            if not any((row_naryad, row_kind, row_material, row_job, row_tech, row_sum3d)):
                continue  # empty lab row
            works.append({
                "source": "lab", "work_order_no": row_naryad, "kind": row_kind,
                "e_value": row_kind, "material_color": row_material, "quantity": row_qty,
                "job_code": row_job, "technician_name": row_tech, "sum3d_id": row_sum3d,
            })
        else:
            if not any((row_client, row_material, row_qty, row_job, row_tech, row_sum3d)):
                continue  # empty client row
            if not row_client:
                return _back(f"Рядок {i + 1}: вкажіть імʼя клієнта.")
            if not row_material:
                return _back(f"Рядок {i + 1}: вкажіть матеріал / колір.")
            works.append({
                "source": "sheet_client", "client_name": row_client,
                "e_value": row_client, "material_color": row_material, "quantity": row_qty,
                "job_code": row_job, "technician_name": row_tech, "sum3d_id": row_sum3d,
            })

    if not works:
        return _back("Заповніть хоча б одну роботу.")

    # Double-submit guard (see _recent_manual_adds): the exact same batch from
    # the same operator inside the window is a resubmit, not a second intent.
    fingerprint = repr((work_type, works))
    now_ts = monotonic()
    last = _recent_manual_adds.get(user.id)
    if last is not None and last[0] == fingerprint and (now_ts - last[1]) < _MANUAL_ADD_DEDUP_SECONDS:
        return RedirectResponse(target, status_code=303)

    # Append the whole batch on the warm write-back worker (cached spreadsheet/
    # worksheet) in ONE sheet call — the cold request thread would re-pay ~40s of
    # open+worksheet through the lab proxy per call. The worker resolves the
    # newest dated tab ≤ today (today's tab often isn't created yet) and returns
    # which tab it actually wrote to, so the orders land on the same day.
    try:
        result = sheet_writeback_pool.submit(
            append_manual_rows_warm, date.today(), works,
            paint_blue=(not is_lab),
            placement=("lab" if is_lab else "client"),
            target_tab=wanted_tab,
        ).result(timeout=120)
    except Exception as exc:  # noqa: BLE001 — surface any sheet failure to the operator
        logger.exception("Manual order sheet write failed")
        return _back(f"Не вдалося записати в таблицю: {exc}")
    if result is None:
        return _back("У таблиці немає жодної датованої вкладки — створіть день у таблиці спершу.")
    tab, note_rows = result

    ensure_seeded(db)
    alias_rows = load_alias_rows(db)
    name_by_id = material_id_by_name(db)
    created_ids: list[int] = []
    for work, note_row in zip(works, note_rows):
        if work["source"] == "lab":
            order = Order(
                source="lab", sheet_tab=tab, row_number=note_row - HEADER_ROWS,
                work_order_no=work["work_order_no"] or None, kind=work["kind"] or None,
                material_color=work["material_color"] or None, quantity=work["quantity"] or None,
                job_code=work["job_code"] or None, technician_name=work["technician_name"] or None,
                sum3d_id=work["sum3d_id"] or None,
                status="прийнято" if work["sum3d_id"] else "нове",
            )
        else:
            order = Order(
                source="sheet_client", sheet_tab=tab, row_number=note_row - HEADER_ROWS,
                client_name=work["client_name"], material_color=work["material_color"] or None,
                quantity=work["quantity"] or None, job_code=work["job_code"] or None,
                technician_name=work["technician_name"] or None,
                sum3d_id=work["sum3d_id"] or None, status="нове",
            )
        order.material_id = resolve_material_id(order.material_color, alias_rows, name_by_id)
        db.add(order)
        db.flush()
        db.add(StatusEvent(order_id=order.id, operator_id=user.id, status=order.status, actor=user.username))
        # Adding a work by hand IS an operator action, so it belongs in the
        # journal and the «Останні дії» popup — otherwise a work the operator
        # just created is the one thing they cannot jump back to. Logged as
        # "create": listed and locatable, but deliberately NOT undoable — «Крок
        # назад» is a quick low-friction click and must never silently delete a
        # row from the shared sheet. Removing a work stays the explicit delete
        # button, which asks first.
        log_action(
            db, order=order, operator=user, action_type="create",
            note=f"додано вручну: {order.work_order_no or order.client_name or ('#' + str(order.id))}",
        )
        created_ids.append(order.id)

    db.add(
        SyncLog(
            direction="db_to_sheet", sheet_tab=tab, status="ok",
            message=f"manual {work_type} ×{len(created_ids)}: рядки {note_rows}",
        )
    )
    db.commit()

    # Record this successful submit so an immediate resubmit (F5/back) is
    # recognised and skipped above. Prune stale entries so the dict can't grow
    # unbounded across a long-running process.
    _recent_manual_adds[user.id] = (fingerprint, now_ts)
    for uid, (_, ts) in list(_recent_manual_adds.items()):
        if now_ts - ts >= _MANUAL_ADD_DEDUP_SECONDS:
            _recent_manual_adds.pop(uid, None)

    return RedirectResponse(target, status_code=303)


@router.post("/orders/{order_id}/focus", response_class=HTMLResponse)
def toggle_order_focus(request: Request, order_id: int, db: Session = Depends(get_db)):
    """Поставити/зняти особисту мітку «беру зараз».

    Відповідь — сам рядок: свап одного <tr> дає миттєвий відгук і не чіпає
    решту таблиці, тобто нічого не стрибає під рукою оператора. Порядок рядків
    мітка не міняє НІКОЛИ — ізоляція набору робиться фільтром «Мої зараз».
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
    return templates.TemplateResponse(request, "_order_row.html", _row_context(request, db, order, None))


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

    order.archived_at = datetime.utcnow()
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

    if order.source in ("lab", "sheet_client") and order.sheet_tab and order.row_number:
        clear_sheet_row_background(order.sheet_tab, order.row_number)
        message = "Роботу видалено з черги, рядок у таблиці очищено"
    else:
        message = "Роботу видалено з черги"

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
    sync_error = write_sheet_fields(db, order, sheet_fields)
    db.commit()
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
async def undo_action(request: Request, action_id: int, db: Session = Depends(get_db)):
    """Undo one specific logged action by id (guards: own action, once, within
    the window, sync not paused), then revert it via _perform_undo."""
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
    if entry.created_at is not None and entry.created_at < datetime.utcnow() - timedelta(seconds=UNDO_WINDOW_SECONDS):
        return toast_response("Вікно скасування минуло", kind="error")
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    return _perform_undo(db, user, entry)


@router.post("/actions/undo-last")
async def undo_last_action(request: Request, db: Session = Depends(get_db)):
    """«Крок назад» — the static undo button. Finds THIS operator's most recent
    still-undoable action (any of UNDOABLE_ACTION_TYPES — Sum3D, status, operator,
    CAM comment, delete — not yet undone, inside the window) and reverts it.
    Pressing it again steps back through earlier actions. Replaces the per-action
    «Скасувати» toast affordance."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    cutoff = datetime.utcnow() - timedelta(seconds=UNDO_WINDOW_SECONDS)
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
async def redo_last_action(request: Request, db: Session = Depends(get_db)):
    """«Крок вперед» — the static redo button. Re-applies THIS operator's most
    recently undone action (any of UNDOABLE_ACTION_TYPES) that is still inside the
    window. Pressing it again steps forward through earlier undos, mirroring
    «Крок назад»."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if sync_control.is_paused():
        return toast_response(SYNC_PAUSED_MSG, kind="info")

    cutoff = datetime.utcnow() - timedelta(seconds=UNDO_WINDOW_SECONDS)
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
        return RedirectResponse("/login", status_code=303)

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    # Archived work (out of the retention window or removed from Google) is a
    # historical record — the passport opens read-only so the operator reviews
    # it (Sum3D, timeline) without editing frozen history.
    read_only = order_is_archived(order, date.today() - timedelta(days=RETENTION_DAYS))

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
        return RedirectResponse("/login", status_code=303)

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
