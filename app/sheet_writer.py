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


def _sheet_row(order: Order) -> int:
    if order.row_number is None:
        raise ValueError(f"order {order.id} has no row_number, can't write back to the sheet")
    return order.row_number + HEADER_ROWS


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


def _next_client_row(
    worksheet: gspread.Worksheet, start_row: int, end_row: int
) -> int:
    """Row directly below the last populated row in the client region — client
    files stack contiguously top-to-bottom, no gap (CLAUDE.md workflow)."""
    raw_rows = call_with_retry(lambda: worksheet.get(f"B{start_row}:E{end_row}"))
    last_filled = start_row - 1
    for offset, row in enumerate(raw_rows):
        b = row[0].strip() if len(row) > 0 else ""
        c = row[1].strip() if len(row) > 1 else ""
        e = row[3].strip() if len(row) > 3 else ""
        if b or c or e:
            last_filled = start_row + offset
    row_number = last_filled + 1
    if row_number > end_row:
        raise RuntimeError(
            f"клієнтська зона заповнена в межах {start_row}-{end_row}"
        )
    return row_number


def _next_lab_row(worksheet: gspread.Worksheet, lab_start: int, lab_end: int) -> int:
    """Row one empty gap below the last lab row (наряд in col B) inside the main
    lab table above the client region — leaves one blank row of separation."""
    raw_rows = call_with_retry(lambda: worksheet.get(f"B{lab_start}:B{lab_end}"))
    last_lab = None
    for offset, row in enumerate(raw_rows):
        b = row[0].strip() if len(row) > 0 else ""
        if b:
            last_lab = lab_start + offset
    row_number = lab_start if last_lab is None else last_lab + 2
    if row_number > lab_end:
        raise RuntimeError(
            f"лабораторна зона заповнена в межах {lab_start}-{lab_end}; "
            "додайте рядки перед клієнтською зоною"
        )
    return row_number


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
    """Append a work row into a dated tab and return its 1-indexed sheet row.

    Shared by manual client AND lab adds (and the email intake). Writes only
    the target cells (never a full-row overwrite), matching the sheet layout:
      * col B "Номер наряду"  — ``work_order_no`` (empty for a client row)
      * col C "Кількість"     — ``quantity``
      * col D "Колір роботи"  — ``material_color``
      * col E "Вид роботи"    — ``e_value`` (client NAME for a client row, work
                                type/вид for a lab row)
      * col I "Номер роботи"  — ``job_code`` work path (only when provided)
      * col J "Ім'я техніка"  — ``technician_name`` (only when provided)
      * col L "ID"            — ``sum3d_id`` (only when provided)

    Placement follows the lab's workflow (CLAUDE.md §3):
      * ``placement="client"`` — appended directly below the last populated row
        in the client region (rows ``start_row``..), stacking contiguously with
        no gap; painted blue by default (pending-client marker).
      * ``placement="lab"`` — placed one empty row below the last lab row (наряд
        in col B) inside the main table above the client region, keeping a blank
        separator; never painted blue.

    ``paint_blue`` fills the row the lab's pending-client blue (client rows
    only). Raises RuntimeError if the target region is full.
    """
    end_row = start_row + max_search_rows
    if placement == "lab":
        row_number = _next_lab_row(
            worksheet, lab_start=HEADER_ROWS + 1, lab_end=CLIENT_REGION_START - 1
        )
    else:
        row_number = _next_client_row(worksheet, start_row, end_row)

    updates = [
        {"range": gspread.utils.rowcol_to_a1(row_number, COL_QUANTITY), "values": [[quantity or ""]]},
        {"range": gspread.utils.rowcol_to_a1(row_number, COL_MATERIAL_COLOR), "values": [[material_color or ""]]},
        {"range": gspread.utils.rowcol_to_a1(row_number, COL_KIND), "values": [[e_value or ""]]},
    ]
    if work_order_no:
        updates.append(
            {"range": gspread.utils.rowcol_to_a1(row_number, COL_WORK_ORDER_NO), "values": [[work_order_no]]}
        )
    if job_code:
        updates.append(
            {"range": gspread.utils.rowcol_to_a1(row_number, COL_JOB_CODE), "values": [[job_code]]}
        )
    if technician_name:
        updates.append(
            {"range": gspread.utils.rowcol_to_a1(row_number, COL_TECHNICIAN), "values": [[technician_name]]}
        )
    if sum3d_id:
        updates.append(
            {"range": gspread.utils.rowcol_to_a1(row_number, COL_SUM3D_ID), "values": [[sum3d_id]]}
        )
    call_with_retry(lambda: worksheet.batch_update(updates))

    # Blue = pending client work (clearing it means issued). Client rows only,
    # best-effort — a formatting hiccup must never fail the write, and read-side
    # detection (app/sheet_colors.py) tolerates a missing fill.
    # Paint only A:K — columns L/M/N (ID, Прорахував, Відфрезерував) keep their
    # own green styling and must not be overwritten blue.
    if paint_blue:
        try:
            call_with_retry(
                lambda: worksheet.format(
                    f"A{row_number}:K{row_number}",
                    {"backgroundColor": {"red": 0.2901961, "green": 0.5254902, "blue": 0.9098039}},
                )
            )
        except Exception:  # noqa: BLE001
            pass

    return row_number


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
