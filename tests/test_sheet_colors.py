from app.sheet_colors import fetch_row_blue_flags, is_blue


def test_is_blue_true_for_lab_blue_fill():
    assert is_blue({"red": 0.62, "green": 0.77, "blue": 0.95}) is True
    assert is_blue({"red": 0.2, "green": 0.5, "blue": 0.9}) is True


def test_is_blue_false_for_no_fill_or_white():
    assert is_blue(None) is False
    assert is_blue({}) is False
    assert is_blue({"red": 1, "green": 1, "blue": 1}) is False


def test_is_blue_false_for_other_hues():
    assert is_blue({"red": 0.4, "green": 0.9, "blue": 0.4}) is False  # green
    assert is_blue({"red": 0.95, "green": 0.6, "blue": 0.2}) is False  # orange
    assert is_blue({"red": 0.9, "green": 0.5, "blue": 0.5}) is False  # red/pink


class _FakeSpreadsheet:
    def __init__(self, colors):
        # colors: list of backgroundColor dicts (or None) per data row
        self._colors = colors

    def fetch_sheet_metadata(self, params):
        row_data = []
        for color in self._colors:
            if color is None:
                row_data.append({"values": [{"effectiveFormat": {}}]})
            else:
                row_data.append(
                    {"values": [{"effectiveFormat": {"backgroundColor": color}}]}
                )
        return {"sheets": [{"data": [{"rowData": row_data}]}]}


class _FakeWorksheet:
    def __init__(self, colors):
        self.title = "22.06.26"
        self.spreadsheet = _FakeSpreadsheet(colors)


def test_fetch_row_blue_flags_maps_row_numbers():
    ws = _FakeWorksheet([
        {"red": 0.62, "green": 0.77, "blue": 0.95},  # row 1 blue
        None,                                          # row 2 no fill
        {"red": 0.4, "green": 0.9, "blue": 0.4},       # row 3 green
    ])
    flags = fetch_row_blue_flags(ws)
    assert flags[1] is True
    assert flags[2] is False
    assert flags[3] is False


def test_fetch_row_blue_flags_degrades_to_empty_on_error():
    class Boom:
        title = "22.06.26"

        class spreadsheet:  # noqa: N801
            @staticmethod
            def fetch_sheet_metadata(params):
                raise RuntimeError("api down")

    assert fetch_row_blue_flags(Boom()) == {}
