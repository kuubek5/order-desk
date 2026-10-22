"""Рядок клієнтської/поштової роботи звіряється ПОВНИМ ключем (22.10.26).

Бойовий випадок 20.10.26 (#6472): рядок поштової роботи людина свідомо
видалила, на його номер ліг інший лист. До цього дня звірка рядка дивилась
лише на ім'я клієнта в колонці E — і якби у вкладці лишився РІВНО ОДИН інший
рядок того самого клієнта (інша робота), CRM «переїхала» б на нього й зняла
заливку чужій роботі. Ключ тепер той самий, що в синку (`sync._row_identity`):
ім'я + матеріал + кількість. Імена тут вигадані.
"""

from types import SimpleNamespace

from app.parser import HEADER_ROWS
from app.sheet_writer import resolve_order_row, resolve_rows_bulk, write_order_fields


def _row(naryad="", qty="", material="", kind="", tech=""):
    # A..J: № | наряд | к-сть | колір | вид/клієнт | 3×дедлайн | номер роботи | технік
    return ["", naryad, qty, material, kind, "", "", "", "", tech]


class FakeSheet:
    """Справжня сітка рядків; рахує звернення до Google."""

    def __init__(self, rows: dict[int, list[str]]):
        self.id = 7
        self.rows = rows
        self.calls: list[str] = []
        self.updates: list[dict] = []

    def _height(self) -> int:
        return max(self.rows) if self.rows else 0

    def get(self, rng):
        self.calls.append(f"get:{rng}")
        if rng == "B:J":
            return [self.rows.get(i, [""] * 10)[1:10] for i in range(1, self._height() + 1)]
        first, last = rng.split(":")
        row = int(first[1:])
        assert first[0] == "B" and last == f"J{row}", rng
        cells = self.rows.get(row, [""] * 10)[1:10]
        return [cells] if any(cells) else []

    def cell(self, row, col):
        self.calls.append(f"cell:{row},{col}")
        values = self.rows.get(row, [])
        return SimpleNamespace(value=values[col - 1] if col - 1 < len(values) else None)

    def col_values(self, col):
        self.calls.append(f"col_values:{col}")
        return [self.rows.get(i, [""] * 10)[col - 1] for i in range(1, self._height() + 1)]

    def batch_update(self, updates, **_):
        self.calls.append("batch_update")
        self.updates.extend(updates)
        return {}


def _mail_order(order_id=6472, row=80, client="Клієнт-А", material="mono a2", qty="2"):
    return SimpleNamespace(
        id=order_id, row_number=row - HEADER_ROWS, source="email", sheet_tab="20.10.26",
        work_order_no=None, client_name=client, material_color=material, quantity=qty,
    )


def test_deleted_row_does_not_relocate_onto_the_only_other_row_of_that_client():
    """Сценарій інциденту: рядок роботи видалено, на його номері — інший лист,
    а в тій самій вкладці лишився РІВНО ОДИН інший рядок цього клієнта (інша
    робота). Раніше ім'я в E давало єдиний збіг → переїзд на чужий рядок."""
    sheet = FakeSheet({
        78: _row(qty="1", material="pmma a1", kind="Клієнт-А"),   # ІНША робота клієнта
        80: _row(qty="1", material="mono a3", kind="Лаб-Б"),      # лист, що ліг на 80
    })
    order = _mail_order()

    assert resolve_order_row(sheet, order) is None
    assert order.row_number == 80 - HEADER_ROWS, "позицію не переписано"


def test_same_client_row_shifted_onto_the_stored_position_is_not_ours():
    """Після видалення сюди зʼїхав сусідній рядок ТОГО САМОГО клієнта —
    ім'я збігається, але робота інша (інший колір)."""
    sheet = FakeSheet({80: _row(qty="2", material="pmma a1", kind="Клієнт-А")})

    assert resolve_order_row(sheet, _mail_order()) is None


def test_stored_row_with_the_full_key_is_confirmed_with_one_read():
    sheet = FakeSheet({80: _row(qty="2", material="MONO A2", kind=" клієнт-а ")})

    assert resolve_order_row(sheet, _mail_order()) == 80
    assert sheet.calls == ["get:B80:J80"], "одне читання, як і раніше"


def test_shifted_row_relocates_on_a_unique_full_key_match():
    sheet = FakeSheet({
        79: _row(qty="2", material="mono a2", kind="Клієнт-А"),   # наш, рядком вище
        80: _row(qty="1", material="pmma a1", kind="Клієнт-А"),   # інша робота
    })
    order = _mail_order()

    assert resolve_order_row(sheet, order) == 79
    assert order.row_number == 79 - HEADER_ROWS


def test_two_rows_with_the_same_full_key_are_ambiguous():
    sheet = FakeSheet({
        81: _row(qty="2", material="mono a2", kind="Клієнт-А"),
        82: _row(qty="2", material="mono a2", kind="Клієнт-А"),
    })

    assert resolve_order_row(sheet, _mail_order()) is None


def test_a_lab_row_is_never_taken_for_a_client():
    """Рядок з нарядом (або техніком) не клієнтський, навіть коли «вид» і
    колір збіглися з клієнтською роботою — як у синку."""
    sheet = FakeSheet({
        79: _row(naryad="24122", qty="2", material="mono a2", kind="Клієнт-А"),
        81: _row(qty="2", material="mono a2", kind="Клієнт-А", tech="Іван"),
    })

    assert resolve_order_row(sheet, _mail_order()) is None


def test_sheet_client_and_nameless_rows_use_the_same_key():
    order = _mail_order(client="", material="pmma a2", qty="6")
    order.source = "sheet_client"
    sheet = FakeSheet({
        79: _row(qty="6", material="pmma a2"),
        80: _row(qty="6", material="pmma a2", kind="Хтось"),
    })

    assert resolve_order_row(sheet, order) == 79


def test_failed_read_is_not_a_confirmation():
    sheet = FakeSheet({80: _row(qty="2", material="mono a2", kind="Клієнт-А")})

    def boom(rng):
        raise RuntimeError("проксі обірвав зʼєднання")

    sheet.get = boom
    assert resolve_order_row(sheet, _mail_order()) is None


def test_quantity_rewrite_is_confirmed_without_the_old_quantity():
    """Правка кількості: у базі вже 3, у таблиці ще 2. Ключ без кількості —
    інакше правка кількості не дійшла б у таблицю ніколи."""
    sheet = FakeSheet({80: _row(qty="2", material="mono a2", kind="Клієнт-А")})
    order = _mail_order(qty="3")
    order.cam_comment = None

    assert write_order_fields(sheet, order, {"quantity"}) is True
    assert sheet.updates == [{"range": "C80", "values": [[3]]}]


def test_other_fields_still_need_the_quantity_to_match():
    sheet = FakeSheet({80: _row(qty="2", material="mono a2", kind="Клієнт-А")})
    order = _mail_order(qty="3")
    order.sum3d_id = "12-01-45"

    assert write_order_fields(sheet, order, {"sum3d_id"}) is False
    assert sheet.updates == []


def test_lab_rows_keep_their_naryad_check():
    sheet = FakeSheet({13: _row(naryad="24122", kind="анатомія", tech="Іван")})
    order = SimpleNamespace(
        id=1, row_number=13 - HEADER_ROWS, source="lab", work_order_no="24122",
        client_name=None, sheet_tab="26.08.26",
    )

    assert resolve_order_row(sheet, order) == 13
    assert sheet.calls == ["cell:13,2"]


def test_bulk_follows_the_same_rule_with_one_read():
    sheet = FakeSheet({
        78: _row(qty="1", material="pmma a1", kind="Клієнт-А"),
        80: _row(qty="1", material="mono a3", kind="Лаб-Б"),
        85: _row(qty="4", material="zr", kind="Клієнт-В"),
    })
    lost = _mail_order()
    other = _mail_order(order_id=6480, row=80, client="Лаб-Б", material="mono a3", qty="1")
    shifted = _mail_order(order_id=6490, row=86, client="Клієнт-В", material="zr", qty="4")

    rows = resolve_rows_bulk(sheet, [lost, other, shifted])

    assert rows == {6472: None, 6480: 80, 6490: 85}
    assert sheet.calls == ["get:B:J"]
