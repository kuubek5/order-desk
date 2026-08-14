"""Turns raw worksheet rows (as returned by gspread's get_all_values) into
structured OrderRow objects.

Column positions and the header/data split (row 7 = first data row) were
confirmed by eye against the test sheet's tab '27.07.26' via app/main.py —
see CLAUDE.md section 3 and 13.
"""

from dataclasses import dataclass, field
from typing import Optional

# Sheet rows 1-6 are headers/labels; data starts at sheet row 7 (index 6).
HEADER_ROWS = 6

# 0-indexed column positions, confirmed against the live sheet dump.
DUE_TIME_COLUMNS = {5: "14:00", 6: "16:00", 7: "09:00"}
BLAME_COLUMNS = {16: "обладнання", 17: "технік", 18: "адміністратор", 19: "клієнт"}


@dataclass
class OrderRow:
    row_number: int  # 1-based position among data rows (sheet row = row_number + HEADER_ROWS)
    seq_no: str
    work_order_no: str
    quantity: str
    material_color: str
    kind: str
    due_time: Optional[str]
    job_code: str
    technician_name: str
    cam_comment: str
    sum3d_id: str
    calculated: str
    milled: str
    last_milled_date: str
    mill_count: str
    rework_blame: dict = field(default_factory=dict)  # blame label -> unit count, only if non-empty
    redo_quantity: str = ""
    redo_cam_comment: str = ""
    redo_sum3d_id: str = ""
    redo_calculated: str = ""
    redo_milled: str = ""

    @property
    def is_empty(self) -> bool:
        # Наряд number is the row's real identifier — operators also jot
        # client name + quantity into these same columns as a pricing
        # placeholder before a наряд is assigned (see CLAUDE.md section 2),
        # so job_code/quantity alone don't mean "this is a real job".
        return not self.work_order_no

    @property
    def is_client_row(self) -> bool:
        """A client work entered straight into the sheet without a наряд number.

        The lab types email clients into the sheet (the "blue-filled" rows):
        no наряд, no technician — the client's name sits in the "вид" column
        (E, parsed here as `kind`) alongside a material and quantity. Confirmed
        on real tabs (Басараб / LekaLab / i3lab Kiev). Internal works always
        get a наряд first, so a наряд-less row carrying a name + material is a
        client placeholder, not a draft internal job — surface it as one."""
        if self.work_order_no:
            return False
        return bool(self.kind and (self.material_color or self.quantity))


def _cell(row: list, idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


def _due_time(row: list) -> Optional[str]:
    # Staff type this on a Ukrainian keyboard, so the mark is often Cyrillic
    # "х" (U+0445), not Latin "x" — the real test sheet confirmed this.
    for idx, label in DUE_TIME_COLUMNS.items():
        if _cell(row, idx).lower() in ("x", "х"):
            return label
    return None


def parse_rows(raw_rows: list[list[str]]) -> list[OrderRow]:
    data_rows = raw_rows[HEADER_ROWS:]
    parsed = []
    for i, row in enumerate(data_rows, start=1):
        order = OrderRow(
            row_number=i,
            seq_no=_cell(row, 0),
            work_order_no=_cell(row, 1),
            quantity=_cell(row, 2),
            material_color=_cell(row, 3),
            kind=_cell(row, 4),
            due_time=_due_time(row),
            job_code=_cell(row, 8),
            technician_name=_cell(row, 9),
            cam_comment=_cell(row, 10),
            sum3d_id=_cell(row, 11),
            calculated=_cell(row, 12),
            milled=_cell(row, 13),
            last_milled_date=_cell(row, 14),
            mill_count=_cell(row, 15),
            rework_blame={
                label: _cell(row, idx) for idx, label in BLAME_COLUMNS.items() if _cell(row, idx)
            },
            redo_quantity=_cell(row, 20),
            redo_cam_comment=_cell(row, 21),
            redo_sum3d_id=_cell(row, 22),
            redo_calculated=_cell(row, 23),
            redo_milled=_cell(row, 24),
        )
        if not order.is_empty or order.is_client_row:
            parsed.append(order)
    return parsed
