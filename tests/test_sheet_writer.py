"""Tests for app.sheet_writer.write_order_fields function.

Tests use mock worksheet objects to ensure no real network calls are made.
"""

import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

import gspread.utils
from app.sheet_writer import write_order_fields
from app.parser import HEADER_ROWS


def make_order(id=1, row_number=1, sheet_tab="27.07.26", sum3d_id="SUM123",
               calculated_raw="+ 10:00", milled_raw=""):
    """Create a minimal fake Order object with the fields sheet_writer.py reads."""
    return SimpleNamespace(
        id=id,
        row_number=row_number,
        sheet_tab=sheet_tab,
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

        write_order_fields(fake_ws, order, {"milled_raw"})

        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["values"] == [[""]]

    def test_empty_string_value_stays_empty(self):
        """Field value empty string should remain empty string."""
        order = make_order(row_number=3, milled_raw="")
        fake_ws = MagicMock()

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

        write_order_fields(fake_ws, order, {"calculated_raw"})

        expected_a1 = gspread.utils.rowcol_to_a1(1 + HEADER_ROWS, 13)
        call_args = fake_ws.batch_update.call_args[0][0]
        assert call_args[0]["range"] == expected_a1

    def test_milled_raw_column_14(self):
        """milled_raw should write to column 14."""
        order = make_order(row_number=1, milled_raw="COL14")
        fake_ws = MagicMock()

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

    def test_mixed_values_including_none(self):
        """Multiple fields with one None value converted to empty string."""
        order = make_order(
            row_number=4,
            sum3d_id="XYZ",
            calculated_raw=None,  # Should become ""
            milled_raw="DONE"
        )
        fake_ws = MagicMock()

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
