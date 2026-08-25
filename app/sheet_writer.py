"""Writes specific fields from the DB back into the Google Sheet.

Only touches cells the operator explicitly changed through the portal — never
a bulk overwrite of a row, so a lab/logist/technician's own edits elsewhere
in that row are left untouched.
"""

from datetime import datetime

import gspread

from app.models import Order
from app.parser import HEADER_ROWS
from app.sheets import call_with_retry

# 1-indexed gspread columns, matching the 0-indexed positions in app/parser.py
# (idx11 -> col 12, idx12 -> col 13, idx13 -> col 14).
COL_SUM3D_ID = 12
COL_CALCULATED = 13
COL_MILLED = 14
COL_CAM_COMMENT = 11
# Rework/БРАК "Заповнює cam оператор" redo ID — sheet column W (parser idx 22).
COL_REDO_SUM3D_ID = 23
STATUS_MARKER_FIELDS = {"calculated_raw", "milled_raw"}

# 1-indexed gspread columns for the mail-intake placeholder row (see
# append_mail_placeholder_row below), matching parser.py's 0-indexed
# work_order_no(1)/quantity(2)/material_color(3)/kind(4).
COL_WORK_ORDER_NO = 2
COL_QUANTITY = 3
COL_MATERIAL_COLOR = 4
COL_KIND = 5
# "Номер роботи" work path (parser idx 8) and "Ім'я техніка" (parser idx 9).
COL_JOB_CODE = 9
COL_TECHNICIAN = 10

# The lab enters mail/client rows below this row; the main lab table lives
# above it (see CLAUDE.md §3 "після 60-го рядка вносимо клієнтів"). Manual adds
# are placed relative to this boundary — lab rows in the table above, client
# rows appended below.
CLIENT_REGION_START = 60

CALCULATED_STATUSES = {
    "прораховано",
    "у фрезеруванні",
    "відфрезеровано",
    "знайдено при видачі",
    "видано",
}
MILLED_STATUSES = {"відфрезеровано", "знайдено при видачі", "видано"}


_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}


def _sheet_row(order: Order) -> int:
    if order.row_number is None:
        raise ValueError(f"order {order.id} has no row_number, can't write back to the sheet")
    return order.row_number + HEADER_ROWS


def _set_row_fills(
    spreadsheet: gspread.Spreadsheet, rows: list[tuple[int, int]], color: dict
) -> None:
    """Paint ONLY the client-name cell (column E — see COL_KIND: for a
    sheet_client row the "вид" column holds the client name) of each
    (sheetId, absolute_sheet_row) to ``color`` in ONE spreadsheets.batchUpdate.
    Rows may span several dated tabs (a client's works aren't always all from
    the same day) since they share one underlying spreadsheet.

    Scoped to the name cell on purpose (user decision 16.08.26): marking a work
    found/issued must recolour only where the client name is, not the whole
    row, so other people's per-cell notes and colours in that row are left
    untouched. The sync still reads the pending flag from column C, so this
    narrower clear never flips a row to "issued" on its own — the portal status
    stays authoritative."""
    if not rows:
        return
    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": COL_KIND - 1,  # column E, 0-based
                    "endColumnIndex": COL_KIND,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        }
        for sheet_id, row in rows
    ]
    call_with_retry(lambda: spreadsheet.batch_update({"requests": requests}))


def clear_row_fills(spreadsheet: gspread.Spreadsheet, rows: list[tuple[int, int]]) -> None:
    """Clear the blue "pending client" fill back to white — the counterpart of
    the blue paint in append_manual_work_rows, used when the lab marks a
    client's work as issued/found ("видано" / "знайдено при видачі")."""
    _set_row_fills(spreadsheet, rows, _WHITE)


def paint_row_fills(spreadsheet: gspread.Spreadsheet, rows: list[tuple[int, int]]) -> None:
    """Repaint the blue "pending client" fill — the inverse of clear_row_fills,
    used when the operator un-marks an accidentally-found work so the sheet
    goes back to "pending" and the next sync doesn't read it as issued."""
    _set_row_fills(spreadsheet, rows, _BLUE)


def clear_placeholder_row(worksheet: gspread.Worksheet, row: int) -> None:
    """Blank a client/mail-placeholder row (A:K values AND the whole A:K fill)
    so a deleted or un-accepted work leaves a row that looks brand-new. Blanking
    (not deleting) keeps every other row's position — and therefore every other
    order's row_number — intact; an all-empty row reads as a free row on the
    next sync, so it's never re-imported. Columns L/M/N are left untouched
    (their green «технік заповнює» styling is the empty-row template), matching
    the write side.

    The fill is cleared across the FULL A:K block, not just the name cell: the
    manual-add paints the blue "pending" fill over all of A:K, so whitening only
    column E left the rest of the row blue after a delete (operator report)."""
    a1 = f"A{row}:{gspread.utils.rowcol_to_a1(row, COL_CAM_COMMENT)}"
    call_with_retry(lambda: worksheet.batch_update([{"range": a1, "values": [[""] * COL_CAM_COMMENT]}]))
    # Whiten A:K — same span the blue "pending" fill was painted over.
    request = {
        "repeatCell": {
            "range": {
                "sheetId": worksheet.id,
                "startRowIndex": row - 1, "endRowIndex": row,
                "startColumnIndex": 0, "endColumnIndex": COL_CAM_COMMENT,  # A:K
            },
            "cell": {"userEnteredFormat": {"backgroundColor": _WHITE}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }
    call_with_retry(lambda: worksheet.spreadsheet.batch_update({"requests": [request]}))


def write_order_fields(worksheet: gspread.Worksheet, order: Order, fields: set[str]) -> None:
    row = _sheet_row(order)
    column_by_field = {
        "cam_comment": COL_CAM_COMMENT,
        "sum3d_id": COL_SUM3D_ID,
        "calculated_raw": COL_CALCULATED,
        "milled_raw": COL_MILLED,
    }

    updates = []
    for field in fields:
        col = column_by_field[field]
        value = getattr(order, field) or ""

        # Status markers are generated from the last DB snapshot, but staff may
        # have filled the shared sheet since that snapshot. Preserve the live
        # value and bring it back into the ORM object instead of overwriting it.
        if field in STATUS_MARKER_FIELDS:
            a1 = gspread.utils.rowcol_to_a1(row, col)
            cell = call_with_retry(lambda a1=a1: worksheet.acell(a1))
            live_value = (cell.value or "").strip()
            if live_value:
                setattr(order, field, live_value)
                continue

        updates.append({"range": gspread.utils.rowcol_to_a1(row, col), "values": [[value]]})

    if updates:
        # Idempotent: retrying writes the same fixed values to the same cells.
        call_with_retry(lambda: worksheet.batch_update(updates))


def write_rework_sum3d(worksheet: gspread.Worksheet, order: Order, value: str) -> None:
    """Write the rework redo Sum3D ID into column W ("Заповнює cam оператор" →
    ID) of the order's row — the second ID column, distinct from the main
    Sum3D ID in column L. Touches only that one cell, never the whole row."""
    row = _sheet_row(order)
    call_with_retry(lambda: worksheet.update_cell(row, COL_REDO_SUM3D_ID, value or ""))


def apply_status_markers(
    order: Order,
    status: str,
    actor: str,
    occurred_at: datetime | None = None,
) -> set[str]:
    """Set missing sheet markers implied by a portal status transition.

    Existing cell content is never replaced: staff may have entered a name or
    timestamp manually in the shared sheet.
    """
    marker_time = occurred_at or datetime.now()
    marker = f"{actor} {marker_time:%H:%M}"
    changed: set[str] = set()

    if status in CALCULATED_STATUSES and not order.calculated_raw:
        order.calculated_raw = marker
        changed.add("calculated_raw")
    if status in MILLED_STATUSES and not order.milled_raw:
        order.milled_raw = marker
        changed.add("milled_raw")

    return changed


def _row_is_occupied(row: list[str]) -> bool:
    """True if a B:E slice belongs to a real work — наряд (B), кількість (C) or
    вид/ім'я клієнта (E) is filled.

    Колір/матеріал (D) is deliberately NOT a signal: the lab also writes loose
    notes there ("залишок матеріалу") on rows that hold no work, and treating
    those as taken would push every add past them."""
    b = row[0].strip() if len(row) > 0 else ""
    c = row[1].strip() if len(row) > 1 else ""
    e = row[3].strip() if len(row) > 3 else ""
    return bool(b or c or e)


def _next_client_row(
    worksheet: gspread.Worksheet, start_row: int, end_row: int
) -> int:
    """Row directly below the last populated row in the client region — client
    files stack contiguously top-to-bottom, no gap (CLAUDE.md workflow)."""
    raw_rows = call_with_retry(lambda: worksheet.get(f"B{start_row}:E{end_row}"))
    last_filled = start_row - 1
    for offset, row in enumerate(raw_rows):
        if _row_is_occupied(row):
            last_filled = start_row + offset
    row_number = last_filled + 1
    if row_number > end_row:
        raise RuntimeError(
            f"клієнтська зона заповнена в межах {start_row}-{end_row}"
        )
    return row_number


def _next_lab_row(worksheet: gspread.Worksheet, lab_start: int, lab_end: int) -> int:
    """Row one empty gap below the last populated lab row inside the main lab
    table above the client region — leaves one blank row of separation.

    Occupancy uses the same B/C/E test as the client region (_row_is_occupied),
    not наряд (B) alone. A lab work may legitimately be written without a наряд
    — it is often filled in later — and such a row was invisible to a B-only
    scan, so every later manual add resolved to that SAME row and overwrote it.
    Column A is excluded on purpose: the lab pre-numbers it 1..N for the whole
    day, so A is never empty and would push every add past the region."""
    raw_rows = call_with_retry(lambda: worksheet.get(f"B{lab_start}:E{lab_end}"))
    last_lab = None
    for offset, row in enumerate(raw_rows):
        if _row_is_occupied(row):
            last_lab = lab_start + offset
    row_number = lab_start if last_lab is None else last_lab + 2
    if row_number > lab_end:
        raise RuntimeError(
            f"лабораторна зона заповнена в межах {lab_start}-{lab_end}; "
            "додайте рядки перед клієнтською зоною"
        )
    return row_number


# Pending-client blue fill (RGB 0..1). Clearing it means the work was issued.
_BLUE = {"red": 0.2901961, "green": 0.5254902, "blue": 0.9098039}


def _row_value_map(work: dict) -> dict[int, str]:
    """1-indexed column → value for one work row. Quantity/material/вид|name are
    always written; наряд/job/tech/Sum3D only when set (so a blank stays blank)."""
    cells = {
        COL_QUANTITY: work.get("quantity") or "",
        COL_MATERIAL_COLOR: work.get("material_color") or "",
        COL_KIND: work.get("e_value") or "",
    }
    for col, value in (
        (COL_WORK_ORDER_NO, work.get("work_order_no")),
        (COL_JOB_CODE, work.get("job_code")),
        (COL_TECHNICIAN, work.get("technician_name")),
        (COL_SUM3D_ID, work.get("sum3d_id")),
    ):
        if value:
            cells[col] = value
    return cells


def _grid_write_requests(
    sheet_id: int, rows: list[int], works: list[dict],
    *, paint_blue: bool, first: int, last: int,
) -> list[dict]:
    """Build low-level Sheets `spreadsheets.batchUpdate` requests that write all
    cell values AND (for client rows) the blue fill in ONE API call — one fewer
    proxy round-trip than a separate values write + format. Only the target
    cells are touched; untouched columns keep their content (no full-row wipe).

    Values go out as ``updateCells`` grouped into contiguous column runs per
    row; the blue fill is one ``repeatCell`` over A:K of the whole block so the
    green ID/mill columns L/M/N are never overwritten."""
    requests: list[dict] = []
    for row_number, work in zip(rows, works):
        cells = _row_value_map(work)
        cols = sorted(cells)
        # split into contiguous column runs so one updateCells writes a run and
        # gaps (e.g. skipped наряд) are left untouched
        run: list[int] = []
        for col in cols:
            if run and col == run[-1] + 1:
                run.append(col)
            else:
                if run:
                    requests.append(_update_cells_run(sheet_id, row_number, run, cells))
                run = [col]
        if run:
            requests.append(_update_cells_run(sheet_id, row_number, run, cells))

    if paint_blue:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": first - 1, "endRowIndex": last,
                    "startColumnIndex": 0, "endColumnIndex": COL_CAM_COMMENT,  # A:K
                },
                "cell": {"userEnteredFormat": {"backgroundColor": _BLUE}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })
    return requests


def _update_cells_run(sheet_id: int, row_number: int, run: list[int], cells: dict[int, str]) -> dict:
    return {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_number - 1, "endRowIndex": row_number,
                "startColumnIndex": run[0] - 1, "endColumnIndex": run[-1],
            },
            "rows": [{"values": [{"userEnteredValue": {"stringValue": cells[c]}} for c in run]}],
            "fields": "userEnteredValue",
        }
    }


def append_manual_work_rows(
    worksheet: gspread.Worksheet,
    works: list[dict],
    *,
    paint_blue: bool = True,
    placement: str = "client",
    start_row: int = CLIENT_REGION_START,
    max_search_rows: int = 200,
) -> list[int]:
    """Append several manual work rows in ONE batch and return their 1-indexed
    sheet rows (aligned with ``works``). Fewer round-trips than N single appends
    — one batch_update for all cells, one blue format for the whole block.

    Each ``works`` item is a dict with ``quantity``/``material_color``/
    ``e_value`` (client NAME or lab вид) and optional ``work_order_no``/
    ``job_code``/``technician_name``/``sum3d_id`` — see column layout in
    append_manual_work_row.

    Placement (CLAUDE.md §3): the whole batch is written as a contiguous block.
      * ``placement="client"`` — block starts directly below the last populated
        row in the client region (no gap), painted blue.
      * ``placement="lab"`` — block starts one empty row below the last lab row
        in the main table (single separator before the block), never painted.
    Raises RuntimeError if the block doesn't fit the target region."""
    if not works:
        return []
    n = len(works)
    end_row = start_row + max_search_rows
    if placement == "lab":
        first = _next_lab_row(
            worksheet, lab_start=HEADER_ROWS + 1, lab_end=CLIENT_REGION_START - 1
        )
        last = first + n - 1
        if last > CLIENT_REGION_START - 1:
            raise RuntimeError(
                f"лабораторна зона не вміщає {n} рядків до клієнтської зони"
            )
    else:
        first = _next_client_row(worksheet, start_row, end_row)
        last = first + n - 1
        if last > end_row:
            raise RuntimeError(f"клієнтська зона не вміщає {n} рядків")

    rows = list(range(first, last + 1))
    # Values + blue fill in ONE spreadsheets.batchUpdate — one fewer proxy
    # round-trip than a separate values write + format. Blue paints only A:K,
    # so the green ID/mill columns (L/M/N) are never overwritten.
    requests = _grid_write_requests(
        worksheet.id, rows, works, paint_blue=paint_blue, first=first, last=last
    )
    call_with_retry(lambda: worksheet.spreadsheet.batch_update({"requests": requests}))

    return rows


def append_manual_work_row(
    worksheet: gspread.Worksheet,
    *,
    work_order_no: str = "",
    quantity: str = "",
    material_color: str = "",
    e_value: str = "",
    job_code: str = "",
    technician_name: str = "",
    sum3d_id: str = "",
    paint_blue: bool = True,
    placement: str = "client",
    start_row: int = CLIENT_REGION_START,
    max_search_rows: int = 200,
) -> int:
    """Append a single work row and return its 1-indexed sheet row. Thin wrapper
    over append_manual_work_rows (email intake + single manual adds); see there
    for the column layout and placement rules."""
    rows = append_manual_work_rows(
        worksheet,
        [{
            "work_order_no": work_order_no,
            "quantity": quantity,
            "material_color": material_color,
            "e_value": e_value,
            "job_code": job_code,
            "technician_name": technician_name,
            "sum3d_id": sum3d_id,
        }],
        paint_blue=paint_blue,
        placement=placement,
        start_row=start_row,
        max_search_rows=max_search_rows,
    )
    return rows[0]


def append_mail_placeholder_row(
    worksheet: gspread.Worksheet,
    client_name: str,
    quantity: str,
    material_color: str,
    start_row: int = 60,
    max_search_rows: int = 200,
) -> int:
    """Client-row convenience wrapper (email intake): client name into "Вид
    роботи", наряд left blank, row painted blue. See append_manual_work_row."""
    return append_manual_work_row(
        worksheet,
        e_value=client_name,
        quantity=quantity,
        material_color=material_color,
        paint_blue=True,
        start_row=start_row,
        max_search_rows=max_search_rows,
    )


def append_order_comment(
    worksheet: gspread.Worksheet,
    order: Order,
    comment_line: str,
) -> str:
    """Append to the live sheet cell so external edits are not overwritten."""
    row = _sheet_row(order)
    a1 = gspread.utils.rowcol_to_a1(row, COL_CAM_COMMENT)
    cell = call_with_retry(lambda: worksheet.acell(a1))
    current = (cell.value or "").strip()
    combined = f"{current}\n{comment_line}" if current else comment_line
    # `combined` is computed once, so a retry re-writes the same absolute value
    # (not a second append) — idempotent even if a prior attempt reached Sheets.
    call_with_retry(lambda: worksheet.update_cell(row, COL_CAM_COMMENT, combined))
    return combined
