"""Tests for app.sheet_writer.write_order_fields function.

Tests use mock worksheet objects to ensure no real network calls are made.
"""

from datetime import datetime

import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

import gspread.utils
from app.sheet_writer import (
    append_mail_placeholder_row,
    append_order_comment,
    apply_status_markers,
    write_order_fields,
)
from app.parser import HEADER_ROWS


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
        fake_ws.batch_update.assert_called_once()
        updates = fake_ws.batch_update.call_args[0][0]
        expected = {
            (gspread.utils.rowcol_to_a1(60, 3), "5"),  # Кількість
            (gspread.utils.rowcol_to_a1(60, 4), "емо а3"),  # Колір роботи
            (gspread.utils.rowcol_to_a1(60, 5), "Вова"),  # Вид роботи
        }
        actual = {(u["range"], u["values"][0][0]) for u in updates}
        assert actual == expected
        assert len(updates) == 3  # точковий запис, не весь рядок

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
        updates = fake_ws.batch_update.call_args[0][0]
        actual = {(u["range"], u["values"][0][0]) for u in updates}
        expected = {
            (gspread.utils.rowcol_to_a1(66, 3), "5"),
            (gspread.utils.rowcol_to_a1(66, 4), "емо а3"),
            (gspread.utils.rowcol_to_a1(66, 5), "Вова"),
        }
        assert actual == expected

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

        with pytest.raises(RuntimeError, match="вільного рядка"):
            append_mail_placeholder_row(fake_ws, "Клієнт", "1", "титан", start_row=60)

        fake_ws.batch_update.assert_not_called()

    def test_custom_start_row_is_respected(self):
        fake_ws = MagicMock()
        fake_ws.get.return_value = []

        row_number = append_mail_placeholder_row(
            fake_ws, "Клієнт", "2", "пмма", start_row=100
        )

        assert row_number == 100
        fake_ws.get.assert_called_once_with("B100:E300")


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
