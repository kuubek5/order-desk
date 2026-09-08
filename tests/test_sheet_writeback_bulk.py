"""Видача клієнта пише в таблицю сталим числом запитів (S2.3).

Поодинці кожна робота коштувала ~4-5 викликів: звірка позиції, читання живих
маркерів, свій batch_update, ще одна звірка перед заливкою. Видача на 50
робіт впиралась у 429 проксі лабораторії — тобто найдовша дія дня ламалась
саме тоді, коли партія велика.

Головне тут — не «швидше», а що пакетний шлях НЕ послабив жодного
запобіжника: непідтверджений рядок і далі пропускається, живе значення
маркера в спільній таблиці й далі виграє в нашого.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

from app.db import Base
from app.models import Order, SyncLog
from app.services import sheet_writeback as wb
from app.sheet_writer import resolve_rows_bulk, write_order_fields_bulk


class FakeWorksheet:
    """Рахує звернення до Google: саме їх, а не секунди, ми й міряємо."""

    def __init__(self, columns: dict[int, list[str]]):
        self.id = 11
        self._columns = columns
        self.calls: list[str] = []
        self.updates: list[dict] = []

    def col_values(self, col):
        self.calls.append(f"col_values:{col}")
        return list(self._columns.get(col, []))

    def batch_update(self, updates):
        self.calls.append("batch_update")
        self.updates.extend(updates)
        return {}


def _client_order(idx: int, row_number: int, name: str) -> Order:
    order = Order(
        source="sheet_client", sheet_tab="07.09.26", row_number=row_number,
        client_name=name, material_color="mono a3", quantity="1", status="знайдено",
        calculated_raw="Р", milled_raw="+",
    )
    order.id = idx
    return order


def test_resolve_rows_bulk_reads_each_column_once():
    names = [""] * 6 + [f"Клієнт{i}" for i in range(1, 21)]  # E: рядки 7..26
    ws = FakeWorksheet({5: names})
    orders = [_client_order(i, i, f"Клієнт{i}") for i in range(1, 21)]

    rows = resolve_rows_bulk(ws, orders)

    assert ws.calls == ["col_values:5"], "одне читання колонки на всю пачку"
    assert rows[1] == 7 and rows[20] == 26


def test_resolve_rows_bulk_relocates_a_shifted_row():
    # Клієнт2 стоїть на рядок вище, ніж пам'ятає база.
    names = [""] * 6 + ["Клієнт2"]
    ws = FakeWorksheet({5: names})
    order = _client_order(2, 5, "Клієнт2")  # збережена позиція → рядок 11

    rows = resolve_rows_bulk(ws, [order])

    assert rows[2] == 7
    assert order.row_number == 1, "позицію виправлено в об'єкті, як і поодинці"


def test_resolve_rows_bulk_refuses_ambiguous_and_missing():
    names = [""] * 6 + ["Двійник", "Двійник"]
    ws = FakeWorksheet({5: names})
    twin = _client_order(1, 20, "Двійник")     # двоє однойменних — неоднозначно
    gone = _client_order(2, 21, "Зниклий")     # у таблиці немає

    rows = resolve_rows_bulk(ws, [twin, gone])

    assert rows[1] is None and rows[2] is None


def test_resolve_rows_bulk_skips_everything_when_the_read_fails():
    """Непідтверджений рядок гірший за пропущений запис — і в пачці теж."""
    ws = FakeWorksheet({})
    ws.col_values = MagicMock(side_effect=RuntimeError("проксі обірвав зʼєднання"))
    orders = [_client_order(i, i, f"Клієнт{i}") for i in range(1, 4)]

    rows = resolve_rows_bulk(ws, orders)

    assert set(rows.values()) == {None}


def test_write_order_fields_bulk_is_one_write_and_keeps_live_markers():
    calculated = [""] * 6 + ["", "вже вписано"]   # рядок 8 персонал заповнив сам
    ws = FakeWorksheet({13: calculated})
    first = _client_order(1, 1, "Клієнт1")        # рядок 7
    second = _client_order(2, 2, "Клієнт2")       # рядок 8
    first.calculated_raw = "наше"
    second.calculated_raw = "наше"

    write_order_fields_bulk(ws, [(first, {"calculated_raw"}, 7), (second, {"calculated_raw"}, 8)])

    assert ws.calls == ["col_values:13", "batch_update"], "одне читання + один запис"
    assert [u["range"] for u in ws.updates] == ["M7"], "живе значення не затирається"
    assert second.calculated_raw == "вже вписано", "живе значення повертається в об'єкт"


def test_write_order_fields_bulk_skips_markers_when_live_read_fails():
    ws = FakeWorksheet({})
    ws.col_values = MagicMock(side_effect=RuntimeError("timeout"))
    order = _client_order(1, 1, "Клієнт1")

    write_order_fields_bulk(ws, [(order, {"calculated_raw"}, 7)])

    assert ws.updates == [], "наосліп маркери не пишемо"


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    # autoflush=False — як у бойовій `writeback_session`: незавершений запис
    # не має тягнутись крізь мережевий виклик і тримати блокування бази.
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(wb, "writeback_session", factory)
    monkeypatch.setattr(wb, "open_spreadsheet", lambda db=None: SimpleNamespace())
    monkeypatch.setattr(wb, "clear_row_fills", lambda ss, rows: None)
    return factory


def test_issue_group_warm_scales_with_the_tab_not_the_batch(db_factory, monkeypatch):
    with db_factory() as db:
        for i in range(1, 13):
            db.add(Order(
                source="sheet_client", sheet_tab="07.09.26", row_number=i,
                client_name=f"Клієнт{i}", material_color="mono a3", quantity="1",
                status="знайдено", calculated_raw="Р",
            ))
        db.commit()
        ids = [o.id for o in db.query(Order).all()]

    names = [""] * 6 + [f"Клієнт{i}" for i in range(1, 13)]
    ws = FakeWorksheet({5: names, 13: [""] * 30})
    monkeypatch.setattr(wb, "get_worksheet_by_name", lambda ss, tab: ws)

    error = wb.issue_group_warm({oid: ["calculated_raw"] for oid in ids})

    assert error is None
    # Дванадцять робіт: одне читання імен, одне читання маркерів, один запис.
    assert ws.calls == ["col_values:5", "col_values:13", "batch_update"]
    assert len(ws.updates) == 12
    with db_factory() as db:
        assert db.query(SyncLog).filter(SyncLog.status == "ok").count() == 12


def test_issue_group_warm_reports_an_unconfirmed_row_as_skipped(db_factory, monkeypatch):
    with db_factory() as db:
        db.add(Order(
            source="sheet_client", sheet_tab="07.09.26", row_number=1,
            client_name="Зниклий", material_color="mono a3", quantity="1",
            status="знайдено", calculated_raw="Р",
        ))
        db.commit()
        order_id = db.query(Order).one().id

    ws = FakeWorksheet({5: [""] * 6 + ["Хтось інший"], 13: [""] * 30})
    monkeypatch.setattr(wb, "get_worksheet_by_name", lambda ss, tab: ws)

    error = wb.issue_group_warm({order_id: ["calculated_raw"]})

    assert error and "не підтверджено" in error
    assert ws.updates == [], "у чужий рядок не пишемо"
    with db_factory() as db:
        entry = db.query(SyncLog).one()
    assert entry.status == "skipped"
