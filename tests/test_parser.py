"""Tests for app/parser.py — OrderRow parsing from raw sheet rows."""

from app.parser import parse_rows

# Exact header structure from the live sheet tab '27.07.26'
HEADER_ROWS_SAMPLE = [
    ['', '', '', '', 'Всього', '0', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', ''],
    ['№', 'Номер наряду', 'Кількість', 'Колір роботи', 'Вид работи', 'Здати до', '', '', 'Номер работи', "Ім'я техніка", 'Коментар для cam оператора (що саме потрібно фрезереувати та т.і.)', 'ID', 'Прорахував', 'Відфрезерував', 'БРАК (переробкаи)', '', '', '', '', '', 'Кількість одиниць фрезерування', 'Комментар для cam оператора (що саме потрібно перефрезерувати і т.і.)', 'ID', 'Прорахував', 'Відфрезерував'],
    ['', '', '', '', '', '', '', '', '', '', '', '', '', '', 'Дата останього фрезерування роботи', 'Який раз фрезерується', 'Хто виненен в переробці спеченої роботи                                                                       (вказати причину)', '', '', '', '', '', '', '', ''],
    ['', '', '', '', '', '14:00', '16:00', '09:00', '', '', '', '', '', '', '', '', 'Обладнання', 'Технік', 'Адміністратор', 'Клієнт', '', '', '', '', ''],
    ['', '', '', '', '', '', '', '', '', '', '', '', '', '', 'Заповнює технік', '', '', '', '', '', '', '', 'Заповнює cam оператора', '', ''],
    ['1', '2', '', '4', '4а', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15', '16', '17', '18', '19', '20', '21', '22', '23', '24'],
]


def make_data_row(fields=None):
    """Build a 25-column data row from sparse {index: value} dict."""
    row = [''] * 25
    if fields:
        for idx, value in fields.items():
            row[idx] = value
    return row


class TestEmptyRowsAreSkipped:
    """Test that rows with all key fields empty are not included in parse results."""

    def test_empty_data_rows_skipped(self):
        """Three fully-blank data rows (only seq_no filled) should be skipped."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({0: '1'}),
            make_data_row({0: '2'}),
            make_data_row({0: '3'}),
        ]
        result = parse_rows(raw_rows)
        assert result == []


class TestFullyPopulatedRow:
    """Test that a complete row with all fields set parses correctly."""

    def test_fully_populated_row_parses_all_fields(self):
        """Construct and parse a row with realistic values at every meaningful index."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                0: '1',                                    # seq_no
                1: '24122',                               # work_order_no
                2: '3',                                   # quantity
                3: 'пмма A2',                             # material_color
                4: 'анатомія',                            # kind
                6: 'x',                                   # due_time: idx6 only marked
                8: '2026-07-21_00016-007',               # job_code
                9: 'Іван',                                # technician_name
                10: 'фрезерувати з запасом',             # cam_comment
                11: 'SUM123',                             # sum3d_id
                12: '+ 10:00',                            # calculated
                13: '+ 14:30',                            # milled
                14: '27.07.26',                           # last_milled_date
                15: '1',                                  # mill_count
                17: '2',                                  # blame: технік
                20: '1',                                  # redo_quantity
                21: 'переробити колір',                   # redo_cam_comment
                22: 'SUM124',                             # redo_sum3d_id
                23: '+ 09:00',                            # redo_calculated
                24: '',                                   # redo_milled
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        row = result[0]

        assert row.row_number == 1
        assert row.seq_no == '1'
        assert row.work_order_no == '24122'
        assert row.quantity == '3'
        assert row.material_color == 'пмма A2'
        assert row.kind == 'анатомія'
        assert row.due_time == '16:00'  # idx6 = 'x' maps to '16:00'
        assert row.job_code == '2026-07-21_00016-007'
        assert row.technician_name == 'Іван'
        assert row.cam_comment == 'фрезерувати з запасом'
        assert row.sum3d_id == 'SUM123'
        assert row.calculated == '+ 10:00'
        assert row.milled == '+ 14:30'
        assert row.last_milled_date == '27.07.26'
        assert row.mill_count == '1'
        assert row.rework_blame == {'технік': '2'}
        assert row.redo_quantity == '1'
        assert row.redo_cam_comment == 'переробити колір'
        assert row.redo_sum3d_id == 'SUM124'
        assert row.redo_calculated == '+ 09:00'
        assert row.redo_milled == ''


class TestDueTimeDetection:
    """Test due_time parsing from the three time-slot columns."""

    def test_due_time_none_when_no_x_marked(self):
        """When no due_time column is marked 'x', due_time should be None."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '24122',  # work_order_no (to avoid is_empty)
                # No 'x' marked in columns 5, 6, or 7
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].due_time is None

    def test_due_time_detection_case_insensitive_lowercase(self):
        """Lowercase 'x' in column 5 should be detected and map to '14:00'."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '24122',
                5: 'x',  # lowercase x in idx5 → '14:00'
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].due_time == '14:00'

    def test_due_time_detection_case_insensitive_uppercase(self):
        """Uppercase 'X' in column 7 should be detected and map to '09:00'."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '24122',
                7: 'X',  # uppercase X in idx7 → '09:00'
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].due_time == '09:00'

    def test_due_time_column_idx6_maps_to_16_00(self):
        """Column 6 marked 'x' should map to '16:00'."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '24122',
                6: 'x',  # idx6 → '16:00'
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].due_time == '16:00'


class TestReworkBlame:
    """Test rework_blame dict construction from blame columns."""

    def test_rework_blame_only_includes_non_empty_columns(self):
        """rework_blame should only include columns with non-empty values."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '24122',
                16: 'обладнання_value',  # idx16 (обладнання)
                # idx17 (технік), idx18 (адміністратор), idx19 (клієнт) are blank
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].rework_blame == {'обладнання': 'обладнання_value'}

    def test_rework_blame_multiple_columns(self):
        """rework_blame should include all non-empty blame columns."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '24122',
                16: 'val_equipment',      # idx16 (обладнання)
                17: 'val_technician',     # idx17 (технік)
                # idx18, idx19 blank
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].rework_blame == {
            'обладнання': 'val_equipment',
            'технік': 'val_technician'
        }

    def test_rework_blame_empty_when_all_columns_blank(self):
        """rework_blame should be an empty dict when all blame columns are blank."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '24122',
                # No blame columns filled
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].rework_blame == {}


class TestRowNumbering:
    """Test that row_number counts data rows only, starting at 1."""

    def test_row_number_counts_data_rows_only_starting_at_1(self):
        """First non-empty data row should have row_number=1, second=2, etc."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({0: '1'}),                    # Skipped (empty)
            make_data_row({0: '2', 1: '24122'}),        # row_number=2
            make_data_row({0: '3', 1: '24123'}),        # row_number=3
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 2
        assert result[0].row_number == 2
        assert result[0].work_order_no == '24122'
        assert result[1].row_number == 3
        assert result[1].work_order_no == '24123'


class TestWhitespaceHandling:
    """Test that _cell strips whitespace from values."""

    def test_cell_values_are_stripped(self):
        """Cell values should be stripped of leading/trailing whitespace."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({
                1: '  24122  ',  # work_order_no with spaces
                9: '  Іван  ',   # technician_name with spaces
            })
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].work_order_no == '24122'
        assert result[0].technician_name == 'Іван'


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_out_of_bounds_column_indices(self):
        """Accessing columns beyond row length should return empty string."""
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({1: '24122'}),  # Only work_order_no filled; rest out of bounds
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        row = result[0]
        assert row.work_order_no == '24122'
        assert row.material_color == ''  # idx3 out of bounds
        assert row.technician_name == ''  # idx9 out of bounds

    def test_job_code_or_quantity_alone_is_still_empty(self):
        """A row with only job_code or only quantity (no work_order_no) is skipped.

        Real test-sheet data showed operators use these same columns to jot a
        client name + quantity as a pricing placeholder before a наряд number
        is assigned (see CLAUDE.md section 2) — those rows aren't real jobs.
        """
        # Row with only job_code
        raw_rows_1 = HEADER_ROWS_SAMPLE + [
            make_data_row({8: 'JOB001'})  # job_code only
        ]
        result_1 = parse_rows(raw_rows_1)
        assert len(result_1) == 0

        # Row with only quantity
        raw_rows_2 = HEADER_ROWS_SAMPLE + [
            make_data_row({2: '5'})  # quantity only
        ]
        result_2 = parse_rows(raw_rows_2)
        assert len(result_2) == 0


class TestClientRows:
    """Наряд-less rows that carry a client name (col 4/вид) + material/quantity
    are the lab's hand-entered email clients — parsed, not skipped."""

    def test_client_row_with_name_and_material_is_parsed(self):
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({2: '1', 3: 'mono a3', 4: 'Басараб'})  # no наряд (idx 1)
        ]
        result = parse_rows(raw_rows)
        assert len(result) == 1
        assert result[0].is_client_row is True
        assert result[0].work_order_no == ''
        assert result[0].kind == 'Басараб'
        assert result[0].material_color == 'mono a3'

    def test_client_name_alone_without_material_or_quantity_is_skipped(self):
        # A lone name in the вид column with nothing else isn't a client work.
        raw_rows = HEADER_ROWS_SAMPLE + [make_data_row({4: 'Басараб'})]
        assert len(parse_rows(raw_rows)) == 0

    def test_normal_row_with_naryad_is_not_a_client_row(self):
        raw_rows = HEADER_ROWS_SAMPLE + [
            make_data_row({1: '24122', 3: 'mono a3', 4: 'анатомія'})
        ]
        result = parse_rows(raw_rows)
        assert result[0].is_client_row is False
