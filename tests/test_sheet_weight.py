"""Замір ваги умовного форматування — діагностика «чому таблиця гальмує».

Копіювання вкладки-дня з учорашньої змушує Google дублювати CF-правила
поклітинково; на тестовій таблиці це дало 105 063 правила й 612 МБ метаданих,
через що КОЖЕН виклик values ставав у рази довшим. Замір лише читає.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.sheets import measure_sheet_weight


def _spreadsheet(payload):
    # Стаб повторює СПРАВЖНЮ структуру gspread 6.x: Spreadsheet.client — це
    # вже сам HTTPClient. Попередній стаб мав зайву ланку .http_client — тобто
    # закріпив мертвий шлях, і тести були зелені, поки бойовий пробник падав
    # AttributeError на кожному виклику (лог 30.08.26).
    client = MagicMock()
    client.fetch_sheet_metadata.return_value = payload
    return SimpleNamespace(id="SHEET", client=client)


def _rule(ranges):
    return {"ranges": ranges}


def _cell(row, col):
    return {"startRowIndex": row, "endRowIndex": row + 1,
            "startColumnIndex": col, "endColumnIndex": col + 1}


def test_counts_rules_per_tab_and_totals():
    sp = _spreadsheet({"sheets": [
        {"properties": {"title": "24.08.26"}, "conditionalFormats": [_rule([_cell(1, 1)])] * 3},
        {"properties": {"title": "25.08.26"}, "conditionalFormats": [_rule([_cell(2, 2)])] * 5},
    ]})
    w = measure_sheet_weight(sp)
    assert w["tab_count"] == 2
    assert w["total_rules"] == 8
    assert w["avg_rules"] == 4.0
    # найважчі вкладки — першими, щоб одразу видно винуватця
    assert w["tabs"][0]["title"] == "25.08.26"


def test_flags_per_cell_duplication():
    """Діапазони 1×1 / 2×1 — відбиток поклітинкового дублювання."""
    sp = _spreadsheet({"sheets": [
        {"properties": {"title": "T"}, "conditionalFormats": [
            _rule([_cell(1, 1), _cell(2, 1)]),
            # здоровий випадок: одне правило на весь стовпець
            _rule([{"startRowIndex": 0, "endRowIndex": 500,
                    "startColumnIndex": 0, "endColumnIndex": 1}]),
        ]},
    ]})
    w = measure_sheet_weight(sp)
    assert w["tiny_ranges"] == 2, "широкий діапазон не має рахуватись як дрібний"


def test_healthy_sheet_reports_small_numbers():
    sp = _spreadsheet({"sheets": [
        {"properties": {"title": "T"}, "conditionalFormats": [_rule([_cell(0, 0)])] * 3},
    ]})
    w = measure_sheet_weight(sp)
    assert w["total_rules"] == 3
    assert w["payload_mb"] >= 0
    assert w["fetch_seconds"] >= 0


def test_tab_without_rules_does_not_crash():
    sp = _spreadsheet({"sheets": [
        {"properties": {"title": "Порожня"}},
        {"properties": {"title": "T"}, "conditionalFormats": []},
    ]})
    w = measure_sheet_weight(sp)
    assert w["total_rules"] == 0
    assert w["avg_rules"] == 0


def test_asks_only_for_conditional_formats():
    """Не тягнути ґрід — інакше сам замір став би тим, що він діагностує."""
    sp = _spreadsheet({"sheets": []})
    measure_sheet_weight(sp)
    params = sp.client.fetch_sheet_metadata.call_args.kwargs["params"]
    assert params["includeGridData"] == "false"
    assert "conditionalFormats" in params["fields"]
