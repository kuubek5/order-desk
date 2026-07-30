"""Writes specific fields from the DB back into the Google Sheet.

Only touches cells the operator explicitly changed through the portal — never
a bulk overwrite of a row, so a lab/logist/technician's own edits elsewhere
in that row are left untouched.
"""

import gspread

from app.models import Order
from app.parser import HEADER_ROWS

# 1-indexed gspread columns, matching the 0-indexed positions in app/parser.py
# (idx11 -> col 12, idx12 -> col 13, idx13 -> col 14).
COL_SUM3D_ID = 12
COL_CALCULATED = 13
COL_MILLED = 14


def _sheet_row(order: Order) -> int:
    if order.row_number is None:
        raise ValueError(f"order {order.id} has no row_number, can't write back to the sheet")
    return order.row_number + HEADER_ROWS


def write_order_fields(worksheet: gspread.Worksheet, order: Order, fields: set[str]) -> None:
    row = _sheet_row(order)
    column_by_field = {
        "sum3d_id": COL_SUM3D_ID,
        "calculated_raw": COL_CALCULATED,
        "milled_raw": COL_MILLED,
    }

    updates = []
    for field in fields:
        col = column_by_field[field]
        value = getattr(order, field) or ""
        updates.append({"range": gspread.utils.rowcol_to_a1(row, col), "values": [[value]]})

    if updates:
        worksheet.batch_update(updates)
