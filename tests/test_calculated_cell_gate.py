"""Ручна літера «Прорахував» (колонка M) — ті самі ворота, що й решта полів
(ревʼю 28.11.26). Своя копія `source in ("lab", "sheet_client")` пропускала
поштову роботу з рядком: у таблицю нічого не йшло, а роут чув «успіх»."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.sheet_writeback import order_writes_to_sheet, write_calculated_cell


class _Db:
    """Лише фіксує, що запис ДІЙШОВ до спроби: таблиця тут недоступна, тож
    спроба закінчується помилкою в SyncLog — саме вона й доказ."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def _order(**kw):
    base = dict(id=1, source="email", sheet_tab="27.11.26", row_number=12)
    base.update(kw)
    return SimpleNamespace(**base)


def test_mail_work_with_a_row_is_written_like_any_other():
    order = _order()
    assert order_writes_to_sheet(order)
    db = _Db()
    error = write_calculated_cell(db, order, "Р")
    assert error, "дійшло до відкриття таблиці (тут — помилка, бо таблиці немає)"
    assert db.added and db.added[0].status == "error"


def test_mail_work_without_a_row_and_rowless_tab_are_skipped():
    for order in (_order(row_number=None), _order(sheet_tab=None)):
        db = _Db()
        assert write_calculated_cell(db, order, "Р") is None
        assert db.added == []


def test_lab_and_typed_client_rows_still_go_through():
    for source in ("lab", "sheet_client"):
        db = _Db()
        assert write_calculated_cell(db, _order(source=source, row_number=None), "Р")
        assert db.added
