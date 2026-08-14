"""Read cell fill colours from a Google Sheets tab.

The lab tracks email-client rows by hand with a BLUE fill: a blue row is a
pending client work, and clearing the blue means the work was issued
("видано"). `get_all_values` only returns text, so telling pending from issued
needs the cell's background colour — fetched here via the Sheets metadata API
(spreadsheets.get with includeGridData), scoped to one column of the data
range so it stays a single cheap call.

Kept separate from app/sheets.py so this heavier per-sync read is opt-in: only
the full sync asks for it (see app/sheet_sync_service.py), never the ~15s hot
lane.
"""

from __future__ import annotations

import gspread

from app.parser import HEADER_ROWS
from app.sheets import call_with_retry

# The whole client row is filled, so one representative column is enough. Col D
# (Колір роботи) is always populated on a real client row and carries the fill.
_FILL_COLUMN_LETTER = "D"
# How far down to scan for fills. Client rows sit well below the technician
# block (~row 60+), but read from the first data row so nothing is missed; a
# few hundred rows is one API response.
_MAX_DATA_ROWS = 400


def is_blue(color: dict | None) -> bool:
    """True for a blue-ish cell fill, False for white / no-fill / other hues.

    Google reports colours as 0..1 floats; a missing key means 0, and an
    absent backgroundColor (or white {1,1,1}) means "no fill". Blue is the
    lab's "pending client" marker — require the blue channel to clearly
    dominate red and green so a green "done" or orange fill never reads as
    blue."""
    if not color:
        return False
    red = color.get("red", 0.0)
    green = color.get("green", 0.0)
    blue = color.get("blue", 0.0)
    if blue < 0.5:
        return False
    # White / near-white (all channels high) is "no fill", not blue.
    if red > 0.8 and green > 0.8:
        return False
    return blue > red + 0.15 and blue > green + 0.1


def fetch_row_blue_flags(worksheet: gspread.Worksheet) -> dict[int, bool]:
    """Map data-row number (1-based, as OrderRow.row_number) -> is the row's
    fill blue. Best-effort: returns {} on any API/shape failure so the caller
    can degrade to "no colour info" rather than break the sync."""
    first_row = HEADER_ROWS + 1
    last_row = HEADER_ROWS + _MAX_DATA_ROWS
    rng = f"{worksheet.title}!{_FILL_COLUMN_LETTER}{first_row}:{_FILL_COLUMN_LETTER}{last_row}"
    params = {
        "includeGridData": True,
        "ranges": [rng],
        "fields": "sheets(data(rowData(values(effectiveFormat(backgroundColor)))))",
    }
    try:
        meta = call_with_retry(lambda: worksheet.spreadsheet.fetch_sheet_metadata(params))
        row_data = meta["sheets"][0]["data"][0].get("rowData", [])
    except Exception:  # noqa: BLE001 — colour is a nicety, never fatal to sync
        return {}

    flags: dict[int, bool] = {}
    for offset, row in enumerate(row_data, start=1):
        values = row.get("values") or []
        color = None
        if values:
            color = (values[0].get("effectiveFormat") or {}).get("backgroundColor")
        flags[offset] = is_blue(color)
    return flags
