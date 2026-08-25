"""Tests for app.sheet_writer.write_order_fields function.

Tests use mock worksheet objects to ensure no real network calls are made.
"""

from datetime import datetime

import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

import gspread.utils
from app.sheet_writer import (
    _BLUE,
    _WHITE,
    append_mail_placeholder_row,
    append_manual_work_row,
    append_manual_work_rows,
    append_order_comment,
    apply_status_markers,
    clear_row_fills,
    paint_row_fills,
    write_order_fields,
)
from app.parser import HEADER_ROWS


def _written(fake_ws):
    """Parse {(row1, col1): value} from the single spreadsheet.batch_update
    call the append writer now makes (values via updateCells requests)."""
    body = fake_ws.spreadsheet.batch_update.call_args[0][0]
    out = {}
    for req in body["requests"]:
        uc = req.get("updateCells")
        if not uc:
            continue
        r0 = uc["range"]["startRowIndex"]
        c0 = uc["range"]["startColumnIndex"]
        for row in uc["rows"]:
            for i, cell in enumerate(row["values"]):
                out[(r0 + 1, c0 + 1 + i)] = cell["userEnteredValue"]["stringValue"]
    return out


def _blue_range(fake_ws):
    """The repeatCell background range from the batch_update, or None."""
    body = fake_ws.spreadsheet.batch_update.call_args[0][0]
    for req in body["requests"]:
        rc = req.get("repeatCell")
        if rc:
            return rc["range"]
    return None


def make_order(id=1, row_number=1, sheet_tab="27.07.26", sum3d_id="SUM123",
               calculated_raw="+ 10:00", milled_raw="", cam_comment=""):
    """Create a minimal fake Order object with the fields sheet_writer.py reads."""
    return SimpleNamespace(
        id=id,
        row_number=row_number,
        sheet_tab=sheet_tab,
        cam_comment=cam_comment,
        sum3d_id=sum3d_id,
        calculated_raw=calculated_raw,
        milled_raw=milled_raw,
    )


class TestWriteOrderFieldsSingleField:
    """Test writing a single field to the worksheet."""

    def test_single_field_write(self):
        """Write sum3d_id field; batch_update called once with correct A1 range."""
        order = make_order(row_number=1, sum3d_id="SUM123")
        fake_ws = MagicMock()

        write_order_fields(fake_ws, order, {"sum3d_id"})

        # Expected sheet row: 1 + HEADER_ROWS = 1 + 6 = 7
        expected_row = 1 + HEADER_ROWS
        expected_col = 12  # COL_SUM3D_ID
        expected_a1 = gspread.utils.rowcol_to_a1(expected_row, expected_col)

        fake_ws.batch_update.assert_called_once()
        call_args = fake_ws.batch_update.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0]["range"] == expected_a1
        assert call_args[0]["values"] == [["SUM123"]]


class TestWriteOrderFieldsMultiple:
    """Test writing multiple fields in a single batch_update call."""

    def test_multiple_fields_write(self):
        """Write sum3d_id and calculated_raw; both entries in single batch_update call."""
        order = make_order(row_number=2, sum3d_id="SUM456", calculated_raw="+ 20:30")
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = ""

        write_order_fields(fake_ws, order, {"sum3d_id", "calculated_raw"})

        fake_ws.batch_update.assert_called_once()
        call_args = fake_ws.batch_update.call_args[0][0]
        assert len(call_args) == 2

        expected_row = 2 + HEADER_ROWS

        # Build expected update dicts to compare as a set (order may vary due to set iteration)
        expected_updates = {
            (gspread.utils.rowcol_to_a1(expected_row, 12), "SUM456"),  # sum3d_id
            (gspread.utils.rowcol_to_a1(expected_row, 13), "+ 20:30"),  # calculated_raw
        }
        actual_updates = {
            (update["range"], update["values"][0][0]) for update in call_args
        }
        assert actual_updates == expected_updates


class TestWriteOrderFieldsEmpty:
    """Test that empty fields set does not call batch_update."""

    def test_empty_fields_set(self):
        """Call with empty set; batch_update should NOT be called."""
        order = make_order()
        fake_ws = MagicMock()

        write_order_fields(fake_ws, order, set())

        fake_ws.batch_update.assert_not_called()


class TestWriteOrderFieldsFalsyValue:
    """Test that falsy field values are converted to empty string."""

    def test_none_value_becomes_empty_string(self):
        """Field value None should be written as empty string, not 'None'."""
        order = make_order(row_number=3, milled_raw=None)
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = ""

        write_order_fields(fake_ws, order, {"milled_raw"})

        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["values"] == [[""]]

    def test_empty_string_value_stays_empty(self):
        """Field value empty string should remain empty string."""
        order = make_order(row_number=3, milled_raw="")
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = ""

        write_order_fields(fake_ws, order, {"milled_raw"})

        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["values"] == [[""]]


class TestWriteOrderFieldsMissingRowNumber:
    """Test that missing row_number raises ValueError."""

    def test_missing_row_number_raises(self):
        """order.row_number=None should raise ValueError."""
        order = make_order(row_number=None)
        fake_ws = MagicMock()

        with pytest.raises(ValueError) as exc_info:
            write_order_fields(fake_ws, order, {"sum3d_id"})

        assert "row_number" in str(exc_info.value)
        assert "can't write back" in str(exc_info.value)
        fake_ws.batch_update.assert_not_called()


class TestWriteOrderFieldsRowOffset:
    """Test that row number offset (row_number + HEADER_ROWS) is applied correctly."""

    def test_row_number_offset_applied(self):
        """Row offset calculation: sheet_row = row_number + HEADER_ROWS."""
        order = make_order(row_number=5, sum3d_id="ABC")
        fake_ws = MagicMock()

        write_order_fields(fake_ws, order, {"sum3d_id"})

        expected_row = 5 + HEADER_ROWS  # 5 + 6 = 11
        expected_a1 = gspread.utils.rowcol_to_a1(expected_row, 12)

        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["range"] == expected_a1

    def test_row_offset_with_header_rows_constant(self):
        """Verify offset uses HEADER_ROWS constant, not hardcoded value."""
        order = make_order(row_number=10)
        fake_ws = MagicMock()

        write_order_fields(fake_ws, order, {"sum3d_id"})

        expected_row = 10 + HEADER_ROWS
        expected_a1 = gspread.utils.rowcol_to_a1(expected_row, 12)

        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["range"] == expected_a1


class TestWriteOrderFieldsColumnMappings:
    """Test that fields map to correct columns."""

    def test_sum3d_id_column_12(self):
        """sum3d_id should write to column 12."""
        order = make_order(row_number=1, sum3d_id="COL12")
        fake_ws = MagicMock()

        write_order_fields(fake_ws, order, {"sum3d_id"})

        expected_a1 = gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 12)
        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["range"] == expected_a1

    def test_calculated_raw_column_13(self):
        """calculated_raw should write to column 13."""
        order = make_order(row_number=1, calculated_raw="COL13")
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = ""

        write_order_fields(fake_ws, order, {"calculated_raw"})

        expected_a1 = gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 13)
        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["range"] == expected_a1

    def test_milled_raw_column_14(self):
        """milled_raw should write to column 14."""
        order = make_order(row_number=1, milled_raw="COL14")
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = ""

        write_order_fields(fake_ws, order, {"milled_raw"})

        expected_a1 = gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 14)
        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["range"] == expected_a1


class TestWriteOrderFieldsIntegration:
    """Integration-style tests combining multiple aspects."""

    def test_all_three_fields_together(self):
        """Write all three supported fields in one call."""
        order = make_order(
            row_number=7,
            sum3d_id="ID789",
            calculated_raw="+ 15:45",
            milled_raw="MILLED"
        )
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = ""

        write_order_fields(fake_ws, order, {"sum3d_id", "calculated_raw", "milled_raw"})

        fake_ws.batch_update.assert_called_once()
        call_args = fake_ws.batch_update.call_args[0][0]
        assert len(call_args) == 3

        expected_row = 7 + HEADER_ROWS
        expected_updates = {
            (gspread.utils.rowcol_to_a1(expected_row, 12), "ID789"),
            (gspread.utils.rowcol_to_a1(expected_row, 13), "+ 15:45"),
            (gspread.utils.rowcol_to_a1(expected_row, 14), "MILLED"),
        }
        actual_updates = {
            (update["range"], update["values"][0][0]) for update in call_args
        }
        assert actual_updates == expected_updates


class TestAdditionalSheetFields:
    def test_cam_comment_uses_column_11(self):
        order = make_order(row_number=1, cam_comment="Перевірити край")
        fake_ws = MagicMock()

        write_order_fields(fake_ws, order, {"cam_comment"})

        expected_a1 = gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 11)
        update = fake_ws.batch_update.call_args[0][0][0]
        assert update["range"] == expected_a1
        assert update["values"] == [["Перевірити край"]]


class TestStatusMarkers:
    def test_calculated_status_sets_calculated_marker(self):
        order = make_order(calculated_raw=None, milled_raw=None)

        fields = apply_status_markers(
            order, "прораховано", "Роман", datetime(2026, 8, 1, 9, 5)
        )

        assert fields == {"calculated_raw"}
        assert order.calculated_raw == "Роман 09:05"
        assert order.milled_raw is None

    def test_milled_status_sets_both_missing_markers(self):
        order = make_order(calculated_raw=None, milled_raw=None)

        fields = apply_status_markers(
            order, "відфрезеровано", "operator", datetime(2026, 8, 1, 17, 30)
        )

        assert fields == {"calculated_raw", "milled_raw"}
        assert order.calculated_raw == "operator 17:30"
        assert order.milled_raw == "operator 17:30"

    def test_existing_manual_markers_are_preserved(self):
        order = make_order(calculated_raw="Іван 10:00", milled_raw="Марія 12:00")

        fields = apply_status_markers(order, "видано", "Роман")

        assert fields == set()
        assert order.calculated_raw == "Іван 10:00"
        assert order.milled_raw == "Марія 12:00"

    def test_problem_status_does_not_create_marker(self):
        order = make_order(calculated_raw=None, milled_raw=None)

        fields = apply_status_markers(order, "проблема", "Роман")

        assert fields == set()

    def test_live_manual_marker_is_not_overwritten(self):
        order = make_order(row_number=1, calculated_raw="Роман 09:05")
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = "Іван 09:04"

        write_order_fields(fake_ws, order, {"calculated_raw"})

        fake_ws.acell.assert_called_once_with(
            gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 13)
        )
        fake_ws.batch_update.assert_not_called()
        assert order.calculated_raw == "Іван 09:04"

    def test_only_empty_live_marker_cells_are_written(self):
        order = make_order(
            row_number=1,
            calculated_raw="Роман 09:05",
            milled_raw="Роман 11:30",
        )
        fake_ws = MagicMock()
        calculated_cell = gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 13)

        def live_cell(a1):
            value = "Іван 09:04" if a1 == calculated_cell else ""
            return SimpleNamespace(value=value)

        fake_ws.acell.side_effect = live_cell

        write_order_fields(fake_ws, order, {"calculated_raw", "milled_raw"})

        updates = fake_ws.batch_update.call_args[0][0]
        assert updates == [
            {
                "range": gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 14),
                "values": [["Роман 11:30"]],
            }
        ]
        assert order.calculated_raw == "Іван 09:04"


class TestAppendMailPlaceholderRow:
    def test_writes_to_start_row_when_immediately_free(self):
        """No pre-existing data in the scan window: the very first row
        (start_row) is used."""
        fake_ws = MagicMock()
        fake_ws.get.return_value = []

        row_number = append_mail_placeholder_row(
            fake_ws, "Вова", "5", "емо а3", start_row=60
        )

        assert row_number == 60
        fake_ws.get.assert_called_once_with("B60:E260")
        fake_ws.spreadsheet.batch_update.assert_called_once()
        assert _written(fake_ws) == {
            (60, 3): "5",       # Кількість
            (60, 4): "емо а3",  # Колір роботи
            (60, 5): "Вова",    # Вид роботи
        }

    def test_skips_occupied_rows_60_to_65_and_uses_66(self):
        """Rows 60-65 already have something in Номер наряду / Кількість /
        Вид роботи (existing manual notes or unrelated data) — the scan
        must not touch them and instead land on the first free row after."""
        fake_ws = MagicMock()
        fake_ws.get.return_value = [
            ["24567", "2", "pmma a2", "Клієнт А"],  # row 60: наряд filled
            ["", "3", "mono a3", "Клієнт Б"],  # row 61: кількість+вид filled
            ["", "", "", "Клієнт В"],  # row 62: вид filled
            ["24999", "", "", ""],  # row 63: наряд filled
            ["", "1", "", ""],  # row 64: кількість filled
            ["", "", "", "Клієнт Г"],  # row 65: вид filled
        ]

        row_number = append_mail_placeholder_row(
            fake_ws, "Вова", "5", "емо а3", start_row=60
        )

        assert row_number == 66
        assert _written(fake_ws) == {
            (66, 3): "5",
            (66, 4): "емо а3",
            (66, 5): "Вова",
        }

    def test_row_with_only_material_color_filled_is_still_treated_as_free(self):
        """Material/color (col D) isn't part of the occupied-row check — only
        Номер наряду, Кількість, Вид роботи are — so a row with just a
        material value is still fair game."""
        fake_ws = MagicMock()
        fake_ws.get.return_value = [["", "", "залишок матеріалу", ""]]

        row_number = append_mail_placeholder_row(
            fake_ws, "Клієнт", "1", "титан", start_row=60
        )

        assert row_number == 60

    def test_raises_when_no_free_row_within_search_window(self):
        fake_ws = MagicMock()
        fake_ws.get.return_value = [["24000", "1", "", "X"] for _ in range(201)]

        with pytest.raises(RuntimeError, match="заповнена"):
            append_mail_placeholder_row(fake_ws, "Клієнт", "1", "титан", start_row=60)

        fake_ws.spreadsheet.batch_update.assert_not_called()

    def test_custom_start_row_is_respected(self):
        fake_ws = MagicMock()
        fake_ws.get.return_value = []

        row_number = append_mail_placeholder_row(
            fake_ws, "Клієнт", "2", "пмма", start_row=100
        )

        assert row_number == 100
        fake_ws.get.assert_called_once_with("B100:E300")


class TestManualPlacement:
    """placement="client" appends contiguously below the last populated row;
    placement="lab" leaves one empty gap below the last lab row (col B) in the
    main table above the client region."""

    def test_client_appends_after_last_row_leaving_no_gap_over_holes(self):
        """Client rows stack under the LAST populated row, not into an earlier
        gap — even if rows 61/62 are empty, a filled row 63 pushes the new row
        to 64 (immediately under the last record)."""
        fake_ws = MagicMock()
        fake_ws.get.return_value = [
            ["", "33", "sfsd", "43423"],  # row 60 filled
            [],                            # row 61 empty
            [],                            # row 62 empty
            ["4434", "55", "herher", ""],  # row 63 filled
        ]

        row_number = append_manual_work_row(
            fake_ws, e_value="Вова", quantity="5", material_color="емо а3",
            placement="client", start_row=60,
        )

        assert row_number == 64  # directly under the last record, no gap-filling
        fake_ws.get.assert_called_once_with("B60:E260")

    def test_client_empty_window_uses_start_row(self):
        fake_ws = MagicMock()
        fake_ws.get.return_value = []

        row_number = append_manual_work_row(
            fake_ws, e_value="Клієнт", quantity="1", material_color="титан",
            placement="client", start_row=60,
        )
        assert row_number == 60

    def test_lab_leaves_one_gap_after_last_lab_row(self):
        """Last lab row is row 30 → new lab row lands at 32, leaving row 31
        blank as a separator. Scans only the lab region, B:E of rows 7..59."""
        fake_ws = MagicMock()
        # B7:E59 = 53 rows; наряд filled in the first 24 (rows 7..30).
        rows = [["24000", "1", "", "анатомія"]] * 24 + [[]] * 29
        fake_ws.get.return_value = rows

        row_number = append_manual_work_row(
            fake_ws, work_order_no="99001", e_value="анатомія",
            quantity="1", material_color="цирконій", placement="lab",
            paint_blue=False,
        )

        assert row_number == 32  # last lab 30, gap 31, write 32
        fake_ws.get.assert_called_once_with("B7:E59")
        assert _blue_range(fake_ws) is None  # lab rows never painted blue
        written = _written(fake_ws)
        assert written[(32, 2)] == "99001"  # наряд col B

    def test_lab_row_without_naryad_still_occupies_its_row(self):
        """A lab row written with no наряд (allowed — наряд is often filled in
        later) must still count as taken. Scanning col B alone made such a row
        invisible, so every later add resolved to it and overwrote it in place.
        Here rows 7..29 hold наряди and row 31 holds a наряд-less work → the
        next add must land at 33, not back on 31."""
        fake_ws = MagicMock()
        rows = [["24000", "1", "", "анатомія"]] * 23  # rows 7..29
        rows.append([])                               # row 30 — separator
        rows.append(["", "", "TEST2", "TEST"])        # row 31 — no наряд
        rows.extend([[]] * 28)                        # rows 32..59
        fake_ws.get.return_value = rows

        row_number = append_manual_work_row(
            fake_ws, e_value="анатомія", quantity="1",
            material_color="цирконій", placement="lab", paint_blue=False,
        )

        assert row_number == 33  # last occupied 31, gap 32, write 33

    def test_client_blue_fill_stops_before_id_columns(self):
        """Blue fill covers A:K only — columns L/M/N (ID, Прорахував,
        Відфрезерував) keep their own green styling (endColumnIndex 11 = A:K)."""
        fake_ws = MagicMock()
        fake_ws.get.return_value = []

        row = append_manual_work_row(
            fake_ws, e_value="Клієнт", quantity="1", material_color="Ti",
            placement="client", start_row=60,
        )

        rng = _blue_range(fake_ws)
        assert rng["startRowIndex"] == row - 1 and rng["endRowIndex"] == row
        assert rng["startColumnIndex"] == 0 and rng["endColumnIndex"] == 11

    def test_lab_empty_table_uses_first_lab_row(self):
        fake_ws = MagicMock()
        fake_ws.get.return_value = []

        row_number = append_manual_work_row(
            fake_ws, work_order_no="1", e_value="вид", quantity="1",
            material_color="x", placement="lab", paint_blue=False,
        )
        assert row_number == 7  # HEADER_ROWS + 1

    def test_lab_region_full_raises(self):
        fake_ws = MagicMock()
        # every row 7..59 has наряд → no gap-room before the client region
        fake_ws.get.return_value = [["24000"]] * 53

        with pytest.raises(RuntimeError, match="лабораторна зона заповнена"):
            append_manual_work_row(
                fake_ws, work_order_no="1", e_value="вид", quantity="1",
                material_color="x", placement="lab", paint_blue=False,
            )
        fake_ws.spreadsheet.batch_update.assert_not_called()


class TestAppendManualWorkRows:
    """Batch append: ONE spreadsheets.batchUpdate carries all cell values plus
    the blue fill for the whole contiguous block."""

    def test_client_block_is_contiguous_and_one_call(self):
        fake_ws = MagicMock()
        fake_ws.get.return_value = [["", "1", "x", "Наявний"]]  # row 60 filled

        rows = append_manual_work_rows(
            fake_ws,
            [
                {"client_name": "A", "e_value": "A", "material_color": "Ti", "quantity": "1"},
                {"client_name": "B", "e_value": "B", "material_color": "emo", "quantity": "2"},
                {"client_name": "C", "e_value": "C", "material_color": "mono", "quantity": "3"},
            ],
            placement="client", start_row=60,
        )

        assert rows == [61, 62, 63]  # contiguous below last filled (60)
        fake_ws.spreadsheet.batch_update.assert_called_once()  # single API call
        # names landed in вид col E of each row
        written = _written(fake_ws)
        assert written[(61, 5)] == "A" and written[(62, 5)] == "B" and written[(63, 5)] == "C"
        # blue covers the whole block A61:K63 (rows 60..63 zero-indexed, cols 0..11)
        rng = _blue_range(fake_ws)
        assert rng["startRowIndex"] == 60 and rng["endRowIndex"] == 63
        assert rng["startColumnIndex"] == 0 and rng["endColumnIndex"] == 11

    def test_lab_block_gap_then_contiguous_no_blue(self):
        fake_ws = MagicMock()
        fake_ws.get.return_value = [["24000"]] * 24  # lab rows 7..30

        rows = append_manual_work_rows(
            fake_ws,
            [
                {"work_order_no": "1", "e_value": "a", "material_color": "x", "quantity": ""},
                {"work_order_no": "2", "e_value": "b", "material_color": "y", "quantity": ""},
            ],
            placement="lab", paint_blue=False,
        )

        assert rows == [32, 33]  # one gap (31) after last lab (30), then contiguous
        assert _blue_range(fake_ws) is None

    def test_empty_works_writes_nothing(self):
        fake_ws = MagicMock()
        rows = append_manual_work_rows(fake_ws, [], placement="client")
        assert rows == []
        fake_ws.get.assert_not_called()
        fake_ws.spreadsheet.batch_update.assert_not_called()


class TestAppendOrderComment:
    def test_appends_to_live_cell_instead_of_stale_order_value(self):
        order = make_order(row_number=2, cam_comment="stale")
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = "Зовнішня правка"

        combined = append_order_comment(fake_ws, order, "[час · Роман] Новий коментар")

        row = 2 + HEADER_ROWS
        cell = gspread.utils.rowcol_to_a1(row, 11)
        fake_ws.acell.assert_called_once_with(cell)
        fake_ws.update_cell.assert_called_once_with(
            row, 11, "Зовнішня правка\n[час · Роман] Новий коментар"
        )
        assert combined.startswith("Зовнішня правка\n")

    def test_empty_live_cell_has_no_leading_newline(self):
        order = make_order(row_number=1)
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = None

        combined = append_order_comment(fake_ws, order, "Коментар")

        assert combined == "Коментар"
        fake_ws.update_cell.assert_called_once_with(1 + HEADER_ROWS, 11, "Коментар")

    def test_mixed_values_including_none(self):
        """Multiple fields with one None value converted to empty string."""
        order = make_order(
            row_number=4,
            sum3d_id="XYZ",
            calculated_raw=None,  # Should become ""
            milled_raw="DONE"
        )
        fake_ws = MagicMock()
        fake_ws.acell.return_value.value = ""

        write_order_fields(fake_ws, order, {"sum3d_id", "calculated_raw", "milled_raw"})

        call_args = fake_ws.batch_update.call_args[0][0]
        expected_row = 4 + HEADER_ROWS

        expected_updates = {
            (gspread.utils.rowcol_to_a1(expected_row, 12), "XYZ"),
            (gspread.utils.rowcol_to_a1(expected_row, 13), ""),  # None -> ""
            (gspread.utils.rowcol_to_a1(expected_row, 14), "DONE"),
        }
        actual_updates = {
            (update["range"], update["values"][0][0]) for update in call_args
        }
        assert actual_updates == expected_updates


class TestWriteReworkSum3d:
    """Redo Sum3D ID goes to column W (23), a single-cell update."""

    def test_writes_redo_id_to_column_w(self):
        from app.sheet_writer import write_rework_sum3d, COL_REDO_SUM3D_ID

        order = make_order(row_number=5)
        fake_ws = MagicMock()

        write_rework_sum3d(fake_ws, order, "SUM-REDO-9")

        expected_row = 5 + HEADER_ROWS
        assert COL_REDO_SUM3D_ID == 23
        fake_ws.update_cell.assert_called_once_with(expected_row, 23, "SUM-REDO-9")
        fake_ws.batch_update.assert_not_called()

    def test_empty_value_clears_cell(self):
        from app.sheet_writer import write_rework_sum3d

        order = make_order(row_number=2)
        fake_ws = MagicMock()

        write_rework_sum3d(fake_ws, order, "")

        fake_ws.update_cell.assert_called_once_with(2 + HEADER_ROWS, 23, "")


class TestRowFills:
    """clear_row_fills / paint_row_fills recolour ONLY the client-name cell
    (column E), not the whole A:K row — user decision 16.08.26. The sync still
    reads the pending flag from column C, so this narrower repaint never flips
    a row to "issued" on its own."""

    def _fake_spreadsheet(self):
        ss = MagicMock()
        return ss

    def _range(self, ss):
        body = ss.batch_update.call_args[0][0]
        return body["requests"][0]["repeatCell"]["range"]

    def _color(self, ss):
        body = ss.batch_update.call_args[0][0]
        rc = body["requests"][0]["repeatCell"]
        return rc["cell"]["userEnteredFormat"]["backgroundColor"]

    def test_clear_targets_only_client_name_column(self):
        ss = self._fake_spreadsheet()
        clear_row_fills(ss, [(42, 63)])
        rng = self._range(ss)
        assert rng["startColumnIndex"] == 4 and rng["endColumnIndex"] == 5  # column E
        assert rng["startRowIndex"] == 62 and rng["endRowIndex"] == 63
        assert self._color(ss) == _WHITE

    def test_paint_targets_only_client_name_column_blue(self):
        ss = self._fake_spreadsheet()
        paint_row_fills(ss, [(42, 63)])
        rng = self._range(ss)
        assert rng["startColumnIndex"] == 4 and rng["endColumnIndex"] == 5  # column E
        assert self._color(ss) == _BLUE

    def test_empty_rows_is_noop(self):
        ss = self._fake_spreadsheet()
        clear_row_fills(ss, [])
        ss.batch_update.assert_not_called()


def test_clear_placeholder_row_whitens_the_whole_ak_block():
    """Deleting a client work must leave a brand-new-looking row: the blue
    "pending" fill is painted over ALL of A:K on add, so the clear has to whiten
    all of A:K too — whitening only the name cell (column E) left the rest of
    the row blue (operator report 26.08.26). L/M/N (green template) untouched."""
    from unittest.mock import MagicMock
    from app.sheet_writer import clear_placeholder_row, _WHITE, COL_CAM_COMMENT

    ws = MagicMock()
    ws.id = 777
    clear_placeholder_row(ws, 63)

    # The fill batch_update is the spreadsheet.batch_update call.
    body = ws.spreadsheet.batch_update.call_args[0][0]
    rc = body["requests"][0]["repeatCell"]
    assert rc["range"]["startColumnIndex"] == 0
    assert rc["range"]["endColumnIndex"] == COL_CAM_COMMENT  # A:K, not just E
    assert rc["range"]["startRowIndex"] == 62 and rc["range"]["endRowIndex"] == 63
    assert rc["cell"]["userEnteredFormat"]["backgroundColor"] == _WHITE


def _lab_order(work_order_no="A", row_number=1, sum3d_id="12-01-45"):
    return SimpleNamespace(
        id=1, row_number=row_number, sheet_tab="27.07.26", source="lab",
        work_order_no=work_order_no, client_name=None,
        cam_comment="", sum3d_id=sum3d_id, calculated_raw="", milled_raw="",
    )


class TestResolveRowGuardsAgainstShift:
    """#4: a stored row_number goes stale when a row above is DELETED in Google
    Sheets (everything below shifts up). write_order_fields must not write onto
    the neighbour that now sits at the old position — it verifies the наряд and
    relocates, or skips when it can't match unambiguously."""

    def test_writes_to_stored_row_when_naryad_still_matches(self):
        from app.sheet_writer import COL_SUM3D_ID
        order = _lab_order(work_order_no="A", row_number=1)  # stored sheet row 7
        ws = MagicMock()
        ws.cell.return_value = SimpleNamespace(value="A")  # row 7 still holds наряд A
        write_order_fields(ws, order, {"sum3d_id"})
        updates = ws.batch_update.call_args[0][0]
        assert updates[0]["range"] == gspread.utils.rowcol_to_a1(7, COL_SUM3D_ID)
        assert order.row_number == 1  # unchanged

    def test_relocates_and_fixes_row_number_after_a_shift(self):
        from app.sheet_writer import COL_SUM3D_ID
        order = _lab_order(work_order_no="A", row_number=2)  # stored sheet row 8
        ws = MagicMock()
        # Row above deleted: наряд A shifted from row 8 up to row 7; row 8 now B.
        ws.cell.return_value = SimpleNamespace(value="B")  # mismatch at stored row
        ws.col_values.return_value = [""] * 6 + ["A", "B"]  # A is at sheet row 7
        write_order_fields(ws, order, {"sum3d_id"})
        updates = ws.batch_update.call_args[0][0]
        assert updates[0]["range"] == gspread.utils.rowcol_to_a1(7, COL_SUM3D_ID)
        assert order.row_number == 1  # corrected (7 - HEADER_ROWS)

    def test_skips_write_when_naryad_is_gone(self):
        order = _lab_order(work_order_no="A", row_number=2)
        ws = MagicMock()
        ws.cell.return_value = SimpleNamespace(value="B")  # mismatch
        ws.col_values.return_value = [""] * 6 + ["X", "B"]  # no наряд A anywhere
        write_order_fields(ws, order, {"sum3d_id"})
        ws.batch_update.assert_not_called()  # never clobber a neighbour

    def test_skips_write_when_naryad_is_ambiguous(self):
        order = _lab_order(work_order_no="A", row_number=2)
        ws = MagicMock()
        ws.cell.return_value = SimpleNamespace(value="B")  # mismatch
        ws.col_values.return_value = [""] * 6 + ["A", "B", "A"]  # two rows carry A
        write_order_fields(ws, order, {"sum3d_id"})
        ws.batch_update.assert_not_called()

    def test_rework_sum3d_also_guarded(self):
        from app.sheet_writer import write_rework_sum3d
        order = _lab_order(work_order_no="A", row_number=2)
        ws = MagicMock()
        ws.cell.return_value = SimpleNamespace(value="B")  # mismatch
        ws.col_values.return_value = [""] * 6 + ["X"]  # gone
        write_rework_sum3d(ws, order, "22-01-02")
        ws.update_cell.assert_not_called()
