"""Read cell fill colours from a Google Sheets tab.

Two fills the lab uses that carry meaning `get_all_values` can't see:

  * BLUE marks a наряд-less email-client row as pending; clearing the blue
    means the work was issued ("видано").
  * GREY marks an SLM (laser-sintering) row entered for stats only — it never
    belongs in the milling queue (see app/sync.py NON_QUEUE_KINDS).

Fetched via the Sheets metadata API (spreadsheets.get with includeGridData),
scoped to one column of the data range so it stays a single cheap call. Kept
separate from app/sheets.py so this per-sync read is opt-in — both the full
sync and the hot lane call it (see app/sheet_sync_service.py); it's cheap
enough now (~0.3s post CF-cleanup) that neither needs to skip it.
"""

from __future__ import annotations

import gspread

from app.parser import HEADER_ROWS
from app.sheets import call_with_retry

# The whole client row is filled, so one representative column is enough.
# Col C (Кількість) — NOT col D: the sheet's conditional-format rule paints a
# green cell over column D when the material is "Ti", and effectiveFormat
# reports that CF result on top of the static blue, which made every Ti client
# row read as "not blue → issued" the moment it was imported. Column C has no
# conditional formatting, so its effective fill is the honest row colour.
_FILL_COLUMN_LETTER = "C"
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


def is_grey(color: dict | None) -> bool:
    """True for a grey fill — the lab's "SLM / not-for-the-queue" marker
    (laser-sintering batches tracked for stats only). Grey means all three
    channels are close to each other (neutral, no hue) and the value sits
    between near-black and near-white; white / no-fill is NOT grey."""
    if not color:
        return False
    red = color.get("red", 0.0)
    green = color.get("green", 0.0)
    blue = color.get("blue", 0.0)
    lo, hi = min(red, green, blue), max(red, green, blue)
    if hi - lo > 0.08:  # has a hue — not neutral
        return False
    return 0.25 <= hi <= 0.93  # dark headers ~0.6, light rows ~0.85; white ≥0.95


def classify_fill(color: dict | None) -> str:
    """'blue' (pending client), 'grey' (SLM/non-queue), or '' (anything else,
    including no fill)."""
    if is_blue(color):
        return "blue"
    if is_grey(color):
        return "grey"
    return ""


def fetch_row_fills(worksheet: gspread.Worksheet) -> dict[int, str]:
    """Map data-row number (1-based, as OrderRow.row_number) -> fill class
    ('blue' / 'grey' / ''). Best-effort: returns {} on any API/shape failure
    so the caller can degrade to "no colour info" rather than break the sync."""
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

    fills: dict[int, str] = {}
    for offset, row in enumerate(row_data, start=1):
        values = row.get("values") or []
        color = None
        if values:
            color = (values[0].get("effectiveFormat") or {}).get("backgroundColor")
        fills[offset] = classify_fill(color)
    return fills
