from app.sheet_colors import classify_fill, fetch_row_fills, is_blue, is_grey


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


def test_is_grey_true_for_lab_slm_fill():
    # the actual SLM row fill used in the sheet
    assert is_grey({"red": 0.6, "green": 0.6, "blue": 0.6}) is True
    assert is_grey({"red": 0.85, "green": 0.85, "blue": 0.85}) is True


def test_is_grey_false_for_no_fill_white_or_near_black():
    assert is_grey(None) is False
    assert is_grey({}) is False
    assert is_grey({"red": 1, "green": 1, "blue": 1}) is False  # white = no fill
    assert is_grey({"red": 0.97, "green": 0.97, "blue": 0.97}) is False  # near-white
    assert is_grey({"red": 0.05, "green": 0.05, "blue": 0.05}) is False  # near-black text


def test_is_grey_false_for_hued_colors():
    assert is_grey({"red": 0.62, "green": 0.77, "blue": 0.95}) is False  # blue
    assert is_grey({"red": 0.4, "green": 0.9, "blue": 0.4}) is False  # green


def test_classify_fill():
    assert classify_fill({"red": 0.62, "green": 0.77, "blue": 0.95}) == "blue"
    assert classify_fill({"red": 0.6, "green": 0.6, "blue": 0.6}) == "grey"
    assert classify_fill({"red": 1, "green": 1, "blue": 1}) == ""
    assert classify_fill(None) == ""


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


def test_fetch_row_fills_maps_row_numbers():
    ws = _FakeWorksheet([
        {"red": 0.62, "green": 0.77, "blue": 0.95},  # row 1 blue
        None,                                          # row 2 no fill
        {"red": 0.6, "green": 0.6, "blue": 0.6},       # row 3 grey (SLM)
    ])
    fills = fetch_row_fills(ws)
    assert fills[1] == "blue"
    assert fills[2] == ""
    assert fills[3] == "grey"


def test_fetch_row_fills_degrades_to_empty_on_error():
    class Boom:
        title = "22.06.26"

        class spreadsheet:  # noqa: N801
            @staticmethod
            def fetch_sheet_metadata(params):
                raise RuntimeError("api down")

    assert fetch_row_fills(Boom()) == {}
