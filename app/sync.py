from dataclasses import dataclass
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.material_catalog import (
    ensure_seeded,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.models import Comment, Order, ReworkRecord, StatusEvent
from app.parser import HEADER_ROWS, OrderRow


# "Вид/колір" values that mark work the lab records for stats only — SLM laser
# sintering and other non-milling services. These rows never belong in the
# milling queue (user decision 15.08.26): they are skipped on import, and an
# already-imported one is deleted by the same not-seen-anymore reconciliation
# that handles cleared rows. The grey row FILL is the second marker for the
# same thing (batch blocks whose D column is empty) — see sync_tab's row_fills.
NON_QUEUE_KINDS = {"слм", "cлм", "елайнери", "моделі", "сканування", "моделювання"}


logger = logging.getLogger(__name__)


def _is_non_queue_row(row: OrderRow, row_fills: dict[int, str] | None) -> bool:
    """Чи це НЕ фрезерна робота (СЛМ / моделі / елайнери / сканування).

    ДВІ ознаки, обидві ТЕКСТОВІ (колір НЕ враховується — правило власника
    01.09.26: «сірий не враховуємо взагалі»):

    1. Явне слово в матеріалі або виді (`NON_QUEUE_KINDS`) — напр. наряд 29203
       з матеріалом «слм».
    2. Клієнтський рядок БЕЗ МАТЕРІАЛУ. У фрезерної роботи матеріал є завжди
       (mono a3 / Zr / pmma / Ti — це те, з чого фрезерують); СЛМ-блок у
       прод-таблиці 31.08 має лише ім'я + кількість, колонка «Колір роботи»
       порожня (перевірено: рядки 683-701). Порожній матеріал у клієнтському
       рядку = друкують/спікають, не фрезерують.

    Історія: спершу виключали за СІРИМ кольором — знімало з черги реальні
    роботи, помилково зафарбовані сірим. Потім тільки за словом «слм» — тоді
    СЛМ без цього слова просочувався в чергу. Ознака «немає матеріалу» ловить
    саме те, чим СЛМ відрізняється по суті.

    ``row_fills`` лишається в підписі (сумісність викликів) — колір тепер
    керує лише «видано» для клієнтських рядків, не виключенням."""
    material = (row.material_color or "").strip()
    kind = (row.kind or "").strip()
    quantity = (row.quantity or "").strip()
    if material.lower() in NON_QUEUE_KINDS or kind.lower() in NON_QUEUE_KINDS:
        return True
    # Клієнтський рядок, у якого НЕМАЄ або матеріалу, або кількості. Фрезерна
    # робота завжди несе і те, і те (з чого фрезерувати + скільки одиниць).
    # СЛМ-блок у прод-таблиці має лише ім'я + одне з двох: або к-сть без
    # матеріалу (683-701), або значення, помилково вписане в чужу колонку
    # (row130: матеріал «4», к-сть порожня — та сама «4», просто зсунута).
    return row.is_client_row and (not material or not quantity)


def _infer_status(row: OrderRow) -> str:
    if row.milled:
        return "відфрезеровано"
    if row.calculated:
        return "прораховано"
    if row.sum3d_id:
        return "прийнято"
    return "нове"


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    # Orders whose sheet row shifted (a row above them was deleted) and were
    # re-linked to their new position instead of being overwritten.
    moved: int = 0
    # >0 when the mass-archive guard HELD this tab's deletions this sync (looks
    # like a bad read OR a real bulk delete — indistinguishable). Surfaced to
    # the operator as a banner offering «Звірити видалення» (force_reconcile).
    held_mass_vanish: int = 0


def _fields(row: OrderRow) -> dict:
    return {
        "work_order_no": row.work_order_no or None,
        "job_code": row.job_code or None,
        "quantity": row.quantity or None,
        "material_color": row.material_color or None,
        "kind": row.kind or None,
        "due_time": row.due_time,
        "technician_name": row.technician_name or None,
        "cam_comment": row.cam_comment or None,
        "sum3d_id": row.sum3d_id or None,
        "calculated_raw": row.calculated or None,
        "milled_raw": row.milled or None,
        "last_milled_date": row.last_milled_date or None,
        "mill_count": row.mill_count or None,
    }


# Fields a TECHNICIAN owns in the sheet, and the word the operator sees when
# one of them is corrected after import. Deliberately excludes everything the
# portal writes back itself (sum3d_id, calculated/milled markers): those return
# from the sheet as "changes" that the operator made seconds earlier, and
# flagging them would bury the real corrections in noise.
TECHNICIAN_EDITED_FIELDS = {
    "work_order_no": "наряд",
    "quantity": "кількість",
    "material_color": "колір",
    "kind": "вид роботи",
    "job_code": "шлях",
    "technician_name": "технік",
    "client_name": "клієнт",
}


# Every sheet-sourced field an order can carry, across BOTH kinds (lab and
# наряд-less client). Used to wipe an order clean when its row is reused for a
# different work, so no field from its previous life lingers. cam_comment stays
# out: comments are their own records, appended, never overwritten here.
_ALL_ROW_FIELDS = (
    "work_order_no", "job_code", "quantity", "material_color", "kind",
    "due_time", "technician_name", "sum3d_id", "calculated_raw", "milled_raw",
    "last_milled_date", "mill_count", "client_name",
)


def _reset_order_for_new_work(order: Order, *, source: str, status: str) -> None:
    """Strip a revived order back to a blank slate for the new work in its row.

    Only the identity that must survive is kept: the DB id, sheet_tab and
    row_number (so history and position stay linked). source/status adopt the
    new work; every content field is cleared here and refilled by the caller's
    field loop from the current row. Also drops the "technician changed" flag —
    it described the OLD work, and would be meaningless on the new one."""
    order.source = source
    order.status = status
    order.material_id = None
    order.sheet_changed_at = None
    order.sheet_changed_fields = None
    for field in _ALL_ROW_FIELDS:
        setattr(order, field, None)


def _client_fields(row: OrderRow) -> dict:
    """Field mapping for a наряд-less client row (see OrderRow.is_client_row).
    The "вид" column (row.kind) holds the CLIENT NAME here, not a work type, so
    it lands in client_name; work_order_no/kind/technician stay empty.

    sum3d_id / calculated_raw / milled_raw ARE read from columns L/M/N like any
    other row: a client work is calculated in Sum3D and milled just like a lab
    work, so the operator stamps «Прорахував» (М) and «Відфрезерував» (N) on it
    too. These are shared read/write-back columns — ignoring them here would (a)
    hide the operator on client rows in the queue and (b) WIPE an М/Sum3D the
    operator just typed on the very next sync. Only наряд/kind/technician stay
    empty (a client row has none of those). Client status still comes from the
    blue fill, not from these markers."""
    return {
        "work_order_no": None,
        "job_code": None,
        "quantity": row.quantity or None,
        "material_color": row.material_color or None,
        "kind": None,
        "due_time": row.due_time,
        "technician_name": None,
        "cam_comment": row.cam_comment or None,
        "sum3d_id": row.sum3d_id or None,
        "calculated_raw": row.calculated or None,
        "milled_raw": row.milled or None,
        "last_milled_date": row.last_milled_date or None,
        "mill_count": None,
        "client_name": row.kind or None,
    }


def _rework_from_row(row: OrderRow) -> dict | None:
    """Rework/БРАК fields for a sheet row, or None when the row records no
    rework. Technicians fill the blame columns (обладнання/технік/адміністратор/
    клієнт → unit count) and the cam operator fills the redo comment / ID /
    прорахував / відфрезерував columns — the presence of any of those marks a
    rework. `occurrence` is the sheet's "який раз фрезерується" count."""
    blame_labels = list(row.rework_blame.keys())
    blame_quantities = [q for q in row.rework_blame.values() if q]
    if not (
        blame_labels
        or row.redo_quantity
        or row.redo_cam_comment
        or row.redo_sum3d_id
        or row.redo_calculated
        or row.redo_milled
    ):
        return None

    occurrence = None
    if row.mill_count and row.mill_count.strip().isdigit():
        occurrence = int(row.mill_count.strip())

    return {
        "occurrence": occurrence,
        "blame": ", ".join(blame_labels) or None,
        "blame_quantity": ", ".join(blame_quantities) or None,
        "redo_quantity": row.redo_quantity or None,
        "cam_comment": row.redo_cam_comment or None,
        "sum3d_id": row.redo_sum3d_id or None,
        "calculated_raw": row.redo_calculated or None,
        "milled_raw": row.redo_milled or None,
    }


def _sync_rework(session: Session, order_id: int, rework: dict | None) -> bool:
    """Upsert the single sheet-sourced ReworkRecord for an order. Reworks come
    only from the sheet today, so at most one record per order is kept and
    matched by order_id — idempotent across repeated syncs. Never deletes: a
    cleared sheet leaves the last recorded rework intact. Returns True if it
    created or changed anything."""
    if rework is None:
        return False

    existing = session.execute(
        select(ReworkRecord).where(ReworkRecord.order_id == order_id)
    ).scalars().first()

    if existing is None:
        session.add(ReworkRecord(order_id=order_id, **rework))
        return True

    changed = False
    for field, value in rework.items():
        if getattr(existing, field) != value:
            setattr(existing, field, value)
            changed = True
    return changed


def _new_sheet_comment(previous: str | None, current: str | None) -> str | None:
    """Return only newly appended sheet text when possible, else a snapshot."""
    previous = (previous or "").strip()
    current = (current or "").strip()
    if not current or current == previous:
        return None
    if previous and current.startswith(previous):
        appended = current[len(previous):].strip()
        return appended or None
    return current


def _should_apply_sheet_status(current: str, inferred: str) -> bool:
    """Apply only forward sheet progress and preserve portal-only states."""
    if current in {"проблема", "переробка", "знайдено при видачі", "видано"}:
        return False

    progress = {
        "нове": 0,
        "прийнято": 1,
        "прораховано": 2,
        "у фрезеруванні": 3,
        "відфрезеровано": 4,
    }
    current_rank = progress.get(current)
    inferred_rank = progress.get(inferred)
    if current_rank is None or inferred_rank is None:
        return current != inferred
    return inferred_rank > current_rank


def _row_identity(row: OrderRow) -> tuple | None:
    """Stable identity of a sheet row, independent of its position.

    A наряд number identifies a lab work; a наряд-less client row is identified
    by client name + material + quantity, which is what the operator typed and
    what makes that row recognisably "the same work" after rows above it are
    deleted. Returns None when the row carries nothing identifying — such a row
    can only ever be matched positionally.
    """
    if row.is_client_row:
        client = (row.kind or "").strip().casefold()
        if not client:
            return None
        return ("client", client, (row.material_color or "").strip().casefold(),
                (row.quantity or "").strip())
    naryad = (row.work_order_no or "").strip()
    if naryad:
        return ("lab", naryad.casefold())
    # наряд-less lab row (pending-lab: технік вніс роботу, доки наряд ще не
    # присвоїли). Без identity такий рядок міг зіставитись ЛИШЕ позиційно, тож
    # видалення рядка над ним зсувало сусіда на його місце й архівувало не ту
    # роботу (виявлено сценарним прогоном). Ідентифікуємо тим, що вписав технік
    # — технік + матеріал + вид + к-сть — у тому ж дусі, що й клієнтський ключ.
    # Неоднозначні дублікати (той самий технік/матеріал/вид/к-сть) падають на
    # позицію, як і раніше.
    tech = (row.technician_name or "").strip().casefold()
    if not tech:
        return None
    return ("labpending", tech, (row.material_color or "").strip().casefold(),
            (row.kind or "").strip().casefold(), (row.quantity or "").strip())


def _order_identity(order: Order) -> tuple | None:
    """`_row_identity` for an already-imported order — must stay in step with it."""
    if order.source == "sheet_client":
        client = (order.client_name or "").strip().casefold()
        if not client:
            return None
        return ("client", client, (order.material_color or "").strip().casefold(),
                (order.quantity or "").strip())
    if order.source != "lab":
        return None
    naryad = (order.work_order_no or "").strip()
    if naryad:
        return ("lab", naryad.casefold())
    tech = (order.technician_name or "").strip().casefold()
    if not tech:
        return None
    return ("labpending", tech, (order.material_color or "").strip().casefold(),
            (order.kind or "").strip().casefold(), (order.quantity or "").strip())


def _relink_moved_rows(existing_by_row: dict[int, Order], rows: list[OrderRow]) -> int:
    """Repoint orders whose row shifted, rewriting `existing_by_row` in place.

    Orders pair to sheet rows by IDENTITY (наряд / client+material+qty /
    technician+material+kind+qty). Within one identity group — usually a single
    row, but sometimes a legitimate repeat work (той самий наряд двічі) or two
    look-alike client/pending-lab rows — pairing is by RELATIVE ORDER: both
    sides sorted by current position, then zipped 1st-with-1st, 2nd-with-2nd.

    Relative order (not absolute row number) is what survives blank rows being
    inserted above or between works: every member of a group shifts down by the
    same amount, so their order holds and each order still pairs with its own
    row instead of two duplicates colliding on an absolute position. Earlier
    this dropped ambiguous groups entirely and let them fall back to absolute
    position — which mixed duplicates up on exactly such an insert. Extra orders
    in a group (a duplicate deleted from the sheet) stay unpaired and are
    archived by the reconciliation; extra sheet rows (a new duplicate) are
    created there. Returns how many orders were repositioned.
    """
    rows_by_key: dict[tuple, list[OrderRow]] = {}
    for row in rows:
        key = _row_identity(row)
        if key is not None:
            rows_by_key.setdefault(key, []).append(row)

    orders_by_key: dict[tuple, list[Order]] = {}
    for order in existing_by_row.values():
        key = _order_identity(order)
        if key is not None:
            orders_by_key.setdefault(key, []).append(order)

    # Pair within each identity group by relative order; collect the orders that
    # actually need to move to a different row.
    movers: list[tuple[Order, int]] = []
    for key, krows in rows_by_key.items():
        korders = orders_by_key.get(key)
        if not korders:
            continue
        krows_sorted = sorted(krows, key=lambda r: r.row_number)
        korders_sorted = sorted(korders, key=lambda o: o.row_number)
        for order, row in zip(korders_sorted, krows_sorted):
            if order.row_number != row.row_number:
                movers.append((order, row.row_number))

    # Two phases so movers that swap slots don't clobber each other: free every
    # mover's old slot first, then place each at its paired row. A non-mover
    # sitting at a target slot must have a different identity (same-identity
    # peers got distinct targets), so its sheet row is gone — evicting it here
    # just lets the reconciliation archive it, which is correct.
    for order, _ in movers:
        if existing_by_row.get(order.row_number) is order:
            existing_by_row.pop(order.row_number, None)
    for order, new_row in movers:
        order.row_number = new_row
        existing_by_row[new_row] = order
    return len(movers)


def sync_tab(
    session: Session,
    sheet_tab: str,
    rows: list[OrderRow],
    row_fills: dict[int, str] | None = None,
    raw_row_count: int | None = None,
    deletion_grace_seconds: float = 120,
    force_reconcile: bool = False,
) -> SyncResult:
    """Import a tab's rows. ``row_fills`` (row_number -> 'blue'/'grey'/'')
    drives ONLY client-row "видано": the lab clears the blue fill once a client's
    work is issued, so a client row whose fill is explicitly CLEARED (white/'')
    is treated as issued. Blue = pending, and GREY is IGNORED for status (a grey
    client row stays "нове") — grey is applied inconsistently in the sheet.

    СЛМ / non-milling rows are decided by TEXT only (NON_QUEUE_KINDS in material
    or kind), NOT by colour — see _is_non_queue_row. Owner's rule (01.09.26):
    «сірий колір не враховуємо взагалі; СЛМ — це матеріал slm». A whole block of
    real client works had been greyed and vanished when grey excluded them.

    None means "no colour info this run" — client rows then just stay
    pending, and only the text marker filters SLM rows.

    ``raw_row_count`` is how many rows the sheet read returned BEFORE parsing
    (headers included). It is what tells a tab that is genuinely empty apart
    from a transient failed read: a real response still carries the HEADER_ROWS
    header block, a proxy hiccup carries nothing. Without it, clearing the last
    row of a tab left its orders in the queue forever — tomorrow's tab usually
    holds one or two rows, so deleting them empties it entirely and the
    empty-read guard below skipped reconciliation. Omit it to keep the old
    "any parsed row proves the read worked" behaviour."""
    result = SyncResult()

    # Preload every existing order for this tab in ONE query instead of a
    # SELECT per row (the old N+1). `all_tab_orders` keeps EVERY order — the
    # positional map below can hold only one per row_number, and earlier bugs
    # (manual-add overwrite, hybrid resurrect) could leave two orders sharing a
    # row_number. Reconciliation must see BOTH: keying only the map dropped the
    # duplicate, so a deleted row archived one order and left its twin hanging
    # in the queue forever ("2 наряди deleted, 1 stayed"). Sorted by id so the
    # map keeps the NEWEST per row deterministically; the rest still reconcile.
    all_tab_orders = list(
        session.execute(
            select(Order).where(Order.sheet_tab == sheet_tab).order_by(Order.id)
        ).scalars()
    )
    existing_by_row = {order.row_number: order for order in all_tab_orders}

    # Load the material catalog once per tab (not per row) to classify each
    # order's free-text colour into a Material category.
    ensure_seeded(session)
    alias_rows = load_alias_rows(session)
    name_to_id = material_id_by_name(session)

    # SLM/stats-only rows are treated as if they weren't in the sheet at all:
    # not imported, and NOT counted as "seen", so an already-imported one is
    # deleted by the reconciliation below exactly like a cleared row. Keep the
    # RAW row count for the empty-read guard below — an all-SLM tab must still
    # reconcile deletions, unlike a genuinely empty (transient proxy) read.
    had_raw_rows = bool(rows) or (
        raw_row_count is not None and raw_row_count >= HEADER_ROWS
    )
    rows = [row for row in rows if not _is_non_queue_row(row, row_fills)]

    # Re-link orders whose row MOVED. Position alone is not a stable key: the
    # comment below used to assume a removed row is *cleared* (neighbours keep
    # their numbers), but deleting a row in Google Sheets SHIFTS everything
    # below up by one. Purely positional matching then quietly rewrote each
    # surviving order with its neighbour's data and archived the wrong one — the
    # deleted work appeared to "still hang" in the queue while showing someone
    # else's numbers, with its status history attached.
    #
    # So: match by a stable identity first (наряд, or client+material for
    # наряд-less client rows), and only fall back to position. Identity is used
    # ONLY when it is unique on both sides within the tab — repeat works legitimately
    # share a наряд, and two clients can order the same material the same day, so
    # an ambiguous key must never win over position.
    # Reconciliation must see EVERY order for the tab, not just the one-per-row
    # map (which drops duplicate row_numbers). Uses the full preload snapshot,
    # taken before the re-link evicts displaced orders from the map.
    tab_orders = all_tab_orders
    matched_ids: set[int] = set()

    moved = _relink_moved_rows(existing_by_row, rows)
    result.moved += moved

    for row in rows:
        # Position within the tab's data rows, after the identity re-link above
        # has corrected any rows that shifted.
        existing = existing_by_row.get(row.row_number)
        if existing is not None and existing.id is not None:
            matched_ids.add(existing.id)

        # A наряд-less client row (blue-filled email client entered by hand)
        # is a different kind of record: source "sheet_client", client name in
        # place of a наряд, and no milling/rework columns to read. Everything
        # else — positional matching, material resolution, deletion — is shared.
        is_client = row.is_client_row
        if is_client:
            fields = _client_fields(row)
            source = "sheet_client"
            rework = None
            # Blue fill = pending; blue CLEARED (white/no fill) = issued. A GREY
            # fill is the lab's own marker (не «видано»), so grey and blue both
            # stay pending — інакше сіра клієнтська робота імпортувалася б як
            # «видано» й не потрапляла в активну чергу (бойовий випадок 01.09.26,
            # коли реальні роботи були помилково сірі). Тільки явно очищена
            # заливка ('') = видано.
            fill = row_fills.get(row.row_number, "blue") if row_fills is not None else "blue"
            issued = row_fills is not None and fill not in ("blue", "grey")
            status = "видано" if issued else "нове"
        else:
            fields = _fields(row)
            source = "lab"
            status = _infer_status(row)
            rework = _rework_from_row(row)

        if existing is None:
            order = Order(source=source, sheet_tab=sheet_tab, row_number=row.row_number, status=status, **fields)
            order.material_id = resolve_material_id(order.material_color, alias_rows, name_to_id)
            session.add(order)
            session.flush()
            session.add(StatusEvent(order_id=order.id, status=status, actor="sync"))
            if row.cam_comment:
                session.add(Comment(order_id=order.id, source="sheet", text=row.cam_comment))
            _sync_rework(session, order.id, rework)
            result.created += 1
            continue

        changed = False

        # Row reused for the OTHER kind of work (client ↔ lab) while the order
        # is still ACTIVE. Un-archiving via a buggy earlier build could leave a
        # hybrid — a sheet_client order carrying a lab наряд, or vice versa —
        # and the plain field loop below never fixes it, because it only writes
        # the new kind's fields and leaves the old kind's behind. A kind flip is
        # unambiguous row-reuse (correcting a наряд never turns a lab row into a
        # client row), so reset to the new shape. Cheap self-heal for rows the
        # 0.3.6–0.3.9 resurrect bug already corrupted, on the next sync.
        if existing.source != source:
            _reset_order_for_new_work(existing, source=source, status=status)
            changed = True

        # A row holding a DIFFERENT work than the archived order brings that row
        # back into the queue: technicians reuse a row that was cleared, and
        # without this the new work is updated onto an order nobody can see —
        # present in the sheet, absent from the queue.
        #
        # Identity, not mere presence, decides. Deleting from the CRM archives
        # the order and blanks its sheet row on the BACKGROUND writer, so a sync
        # tick (every 15s) routinely reads the row while the blanking is still
        # in flight. Resurrecting on presence alone made every delete bounce
        # straight back — the row still carried the same work. Comparing
        # identity keeps that case archived while still reviving a row that now
        # holds someone else's work.
        # Retention does NOT come through here: old days leave the queue via a
        # date cutoff (RETENTION_DAYS in web.py), never by stamping archived_at,
        # so a full-history import cannot resurrect them.
        if existing.archived_at is not None:
            if is_client:
                was = (existing.client_name or "").strip().casefold()
                now_in_sheet = (fields.get("client_name") or "").strip().casefold()
            else:
                was = (existing.work_order_no or "").strip()
                now_in_sheet = (fields.get("work_order_no") or "").strip()
            if now_in_sheet and now_in_sheet == was:
                # ТА САМА робота стоїть у таблиці, а замовлення в архіві. Так
                # виглядає помилкова архівація (обірване читання, разовий збій
                # кольорів): бойовий випадок 30.08.26 — синк одним тіком
                # заархівував 17 клієнтських робіт, які нікуди з таблиці не
                # зникали, і жоден наступний синк їх не повертав, бо ця гілка
                # воскрешала лише ІНШУ роботу.
                # Умова «інша робота» існувала через гонку з фоновим бланкером
                # (видалення з CRM чистить рядок таблиці у фоні, і синк встигав
                # прочитати ще не почищений рядок). Тому воскрешаємо ту саму
                # роботу ЛИШЕ коли архівації більше 10 хвилин: бланкінг за цей
                # час давно завершився б, отже робота в таблиці — це правда, а
                # не хвіст видалення.
                archived_for = datetime.utcnow() - existing.archived_at
                if archived_for > timedelta(minutes=10):
                    existing.archived_at = None
                    session.add(
                        StatusEvent(
                            order_id=existing.id, status=existing.status, actor="sync",
                            note="рядок і далі в таблиці — повернуто з архіву",
                        )
                    )
                    changed = True
            elif now_in_sheet and now_in_sheet != was:
                # A genuinely different work now occupies this row — reset the
                # revived order to the NEW work's shape completely, don't merge.
                # Merging left the old kind's fields behind: a deleted CLIENT row
                # reused for a LAB наряд kept source="sheet_client" and the old
                # client_name, so the operator saw a hybrid ("частково моя
                # робота"). _reset_order_for_new_work clears every type-specific
                # field and adopts the new source/status before the field loop
                # below fills in the new values.
                _reset_order_for_new_work(existing, source=source, status=status)
                existing.archived_at = None
                session.add(
                    StatusEvent(
                        order_id=existing.id, status=status, actor="sync",
                        note="у рядок вписано іншу роботу — повернуто в чергу",
                    )
                )
                changed = True

        sheet_comment = _new_sheet_comment(existing.cam_comment, row.cam_comment)
        edited: list[str] = []
        for field, value in fields.items():
            if getattr(existing, field) != value:
                # First import of a field the row simply did not have yet (the
                # technician filling in the шлях later, us reading a column for
                # the first time) is not a correction — flagging it would make
                # the badge routine noise and train the operator to ignore it.
                was_filled = bool(getattr(existing, field))
                # The sheet is the source of truth for sum3d_id: an EMPTY column L
                # must clear the DB value, because "можна брати" (takeable) is
                # exactly job_code present + Sum3D empty. Staff clear L in the sheet
                # to hand a work back to the queue, so a fill-only guard here
                # stranded every such work in "В роботі" — it stopped appearing as
                # takeable (regression from 0.3.15, reverted). The rare failed
                # write-back that this exposes (operator's Sum3D lost if the write
                # to L drops) is recoverable by re-entering it; keeping the sheet
                # authoritative is the invariant the whole queue rests on.
                setattr(existing, field, value)
                changed = True
                if was_filled and field in TECHNICIAN_EDITED_FIELDS:
                    edited.append(TECHNICIAN_EDITED_FIELDS[field])

        if edited:
            # Keep any still-undismissed change visible: the operator must see
            # everything that moved since they last acknowledged, not only the
            # latest edit.
            previous = [
                part.strip()
                for part in (existing.sheet_changed_fields or "").split(",")
                if part.strip()
            ]
            merged = previous + [name for name in edited if name not in previous]
            existing.sheet_changed_fields = ", ".join(merged)[:400]
            existing.sheet_changed_at = datetime.utcnow()
            session.add(
                StatusEvent(
                    order_id=existing.id, status=existing.status, actor="sync",
                    note=f"технік змінив у таблиці: {', '.join(edited)}",
                )
            )

        # Re-resolve material when the colour text changed (or was never
        # resolved). Only overwrite with a confident hit — never wipe a good
        # material_id because a colour momentarily became unrecognizable.
        resolved_material = resolve_material_id(existing.material_color, alias_rows, name_to_id)
        if resolved_material is not None and resolved_material != existing.material_id:
            existing.material_id = resolved_material
            changed = True

        if sheet_comment:
            session.add(Comment(order_id=existing.id, source="sheet", text=sheet_comment))

        # The sheet can only represent progress through milling. Portal-only
        # handout states must survive the next read from the sheet.
        if _should_apply_sheet_status(existing.status, status):
            existing.status = status
            session.add(StatusEvent(order_id=existing.id, status=status, actor="sync"))
            changed = True

        if _sync_rework(session, existing.id, rework):
            changed = True

        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    # Removal: a lab order whose row_number no longer appears in the sheet was
    # deleted (or cleared) by the technician — the sheet is the source of truth,
    # so drop it from the queue (cascade removes its history/comments/rework).
    # row_number is the absolute raw-sheet position (parser numbers before
    # filtering blanks), so a cleared row leaves its neighbours' numbers intact
    # and only the cleared row goes missing.
    #
    # Guarded by the RAW read being non-empty (had_raw_rows, before the SLM
    # filter) — a transient empty read (the lab PC's TLS proxy occasionally
    # returns just headers) must never wipe a whole tab, but a tab that's
    # legitimately all-SLM this sync must still reconcile deletions, not be
    # mistaken for that transient case. Only sheet-sourced orders are eligible
    # ("lab" work rows and "sheet_client" client rows) — IMAP "email" orders
    # never live in a sheet tab and must not be touched here.
    # Grace period against a read/write race: a manual add reads the sheet's
    # free row, writes it, and only then commits the Order — while a hot-lane
    # tick that fetched get_all_values a moment EARLIER may reach this
    # reconciliation with rows that predate that write. Without the grace it
    # would delete the freshly created order (the row "isn't in the sheet"),
    # and the next tick would re-import it as a new Order, losing its
    # StatusEvent history and flashing in the UI. Orders younger than the
    # grace window are simply not eligible for deletion; a genuinely removed
    # row still gets reconciled by any sync after the window.
    #
    # A MANUAL sync passes deletion_grace_seconds=0: the operator deleted a row
    # in the sheet and clicked "sync now", so they want it gone now, not in two
    # minutes. That deliberate action isn't racing the CRM manual-add path the
    # grace protects — only the 15s background poll is, and it keeps the grace.
    # This was the "delete from sheet, stays in CRM, manual sync no help" report:
    # a just-imported наряд deleted seconds later sat inside the grace, and every
    # manual sync in that window skipped it.
    grace_cutoff = datetime.utcnow() - timedelta(seconds=deletion_grace_seconds)
    if had_raw_rows:
        # ЗАПОБІЖНИК ВІД МАСОВОЇ АРХІВАЦІЇ. Техніки чистять рядки по одному-два;
        # коли за один тік «зникає» чверть вкладки — це майже напевно не
        # видалення, а погане читання (обірваний респонс, разовий збій
        # заливок), і архівувати за ним означає зняти з черги живі роботи.
        # Бойовий випадок 30.08.26: один тік заархівував 17 клієнтських робіт,
        # які стояли в таблиці неторкані. Поріг: більше 5 робіт І більше 25%
        # активних рядків вкладки — тік пропускає реконсиляцію видалень цілком
        # і голосно пише про це в лог.
        #
        # ``force_reconcile`` (свідома дія оператора «я справді видалив пачку —
        # звір зараз») обходить цей поріг: обірване читання й справжнє масове
        # видалення з одного читання не розрізнити, тому рішення віддане людині,
        # яка знає правду. Помилка самозагоюється — рядок, що досі в таблиці,
        # воскресає наступним синком через 10 хв (гілка вище).
        active = [
            o for o in tab_orders
            if o.source in ("lab", "sheet_client") and o.archived_at is None
        ]
        vanished = [
            o for o in active
            if not (o.id is not None and o.id in matched_ids)
        ]
        mass_vanish = len(vanished) > 5 and active and len(vanished) > 0.25 * len(active)
        if mass_vanish and not force_reconcile:
            result.held_mass_vanish = len(vanished)
            logger.warning(
                "Синк %s: %d із %d рядків «зникли» за один тік — схоже на "
                "обірване читання, архівацію пропущено (оператор може підтвердити "
                "масове видалення діею «звірити видалення»)",
                sheet_tab, len(vanished), len(active),
            )
        else:
            for order in tab_orders:
                # "Matched" beats "its number is present": after a row above it
                # was deleted, a vanished order's OLD number is occupied by the
                # row that shifted up, so presence proves nothing.
                if order.id is not None and order.id in matched_ids:
                    continue
                if order.source not in ("lab", "sheet_client"):
                    continue
                if (
                    deletion_grace_seconds > 0
                    and order.created_at is not None
                    and order.created_at > grace_cutoff
                ):
                    continue
                if order.archived_at is not None:
                    continue  # already archived — don't re-stamp on every sync
                # Keep, don't delete: a row cleared/removed in the sheet leaves
                # the working queue but is preserved for the Archive (the lab
                # prunes old rows/tabs for space, which must never lose our
                # copy). Aged-out active orders drop from the queue by date;
                # this marks the ones removed EARLY.
                order.archived_at = datetime.utcnow()
                result.deleted += 1

    return result
