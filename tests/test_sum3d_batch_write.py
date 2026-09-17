# -*- coding: utf-8 -*-
"""Масовий ввід Sum3D: пачка замість черги, і жодного тихо втраченого ID.

Навіщо. Оператор вписує Sum3D десятку робіт підряд. Поодинці кожна правка
коштувала ДВОХ звернень до Google (звірити позицію рядка + записати), а пул
запису один — тож правки шикувались у чергу й «фіксувались по одній» по 1–4 с
(скарга власника 17.09.26, підтверджено рядками «Slow request: POST
/orders/NNN/sum3d-id took 4.097s» у лозі цеху).

Що стережемо:
1. пачка справді пакетна — N робіт коштують ОДНОГО звіряння позицій і ОДНОГО
   запису, а не N×2;
2. звірка позицій НЕ послаблена: рядок, який не підтвердився, пропускається, а
   не пишеться навмання (інакше ID поїде в чужу роботу);
3. позначка «ще не в таблиці» знімається ЛИШЕ на підтверджений запис, а на
   пропуску й на збої лишається — її підбирає фоновий повтор.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order, SyncLog
from app.services import sheet_writeback as wb


def _engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return engine


def _orders(db: Session, count: int, tab: str = "17.09.26") -> list[int]:
    ids = []
    for i in range(count):
        order = Order(
            source="lab", sheet_tab=tab, row_number=i + 1,
            work_order_no=f"3100{i}", quantity="1", material_color="моно а3",
            sum3d_id=f"12-00-0{i}", sum3d_pending=f"12-00-0{i}",
        )
        db.add(order)
        ids.append(order)
    db.commit()
    return [o.id for o in ids]


def _wire(monkeypatch, engine, *, rows_ok=True, write_raises=False):
    """Підміняємо рівно межу з Google: відкриття таблиці, звірку й запис."""
    monkeypatch.setattr(
        wb, "writeback_session",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    calls = {"resolve": 0, "write": 0, "plans": []}

    worksheet = SimpleNamespace(id=1, title="17.09.26")
    monkeypatch.setattr(wb, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(wb, "get_worksheet_by_name", lambda ss, name: worksheet)

    def fake_resolve(ws, orders):
        calls["resolve"] += 1
        if not rows_ok:
            return {order.id: None for order in orders}
        return {order.id: order.row_number + 6 for order in orders}

    def fake_write(ws, plan):
        calls["write"] += 1
        calls["plans"].append([(order.id, set(fields), row) for order, fields, row in plan])
        if write_raises:
            raise RuntimeError("Google сказав ні")

    monkeypatch.setattr(wb, "resolve_rows_bulk", fake_resolve)
    monkeypatch.setattr(wb, "write_order_fields_bulk", fake_write)
    return calls


def test_ten_edits_cost_one_check_and_one_write(monkeypatch):
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        ids = _orders(db, 10)
    calls = _wire(monkeypatch, engine)

    error = wb.write_fields_bulk({oid: ({"sum3d_id"}, set()) for oid in ids})

    assert error is None, error
    assert calls["resolve"] == 1, "позиції звірялись поодинці — пачки немає"
    assert calls["write"] == 1, "записів більше одного — пачки немає"
    assert len(calls["plans"][0]) == 10


def test_a_confirmed_write_clears_the_not_in_sheet_mark(monkeypatch):
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        ids = _orders(db, 3)
    _wire(monkeypatch, engine)

    wb.write_fields_bulk({oid: ({"sum3d_id"}, set()) for oid in ids})

    with Session(engine) as db:
        assert [db.get(Order, oid).sum3d_pending for oid in ids] == [None, None, None]


def test_an_unconfirmed_row_is_skipped_and_keeps_its_mark(monkeypatch):
    """Рядок, позицію якого не підтверджено, НЕ пишеться — інакше ID поїхав би
    в чужу роботу. Позначка лишається, і повтор спробує ще раз."""
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        ids = _orders(db, 2)
    calls = _wire(monkeypatch, engine, rows_ok=False)

    error = wb.write_fields_bulk({oid: ({"sum3d_id"}, set()) for oid in ids})

    assert error and "не підтверджено" in error
    assert calls["write"] == 0, "писали в непідтверджений рядок"
    with Session(engine) as db:
        assert all(db.get(Order, oid).sum3d_pending for oid in ids)
        skipped = db.query(SyncLog).filter(SyncLog.status == "skipped").count()
        assert skipped == 2, "пропуск не лишив сліду в журналі синку"


def test_a_failed_batch_keeps_every_mark(monkeypatch):
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        ids = _orders(db, 4)
    _wire(monkeypatch, engine, write_raises=True)

    error = wb.write_fields_bulk({oid: ({"sum3d_id"}, set()) for oid in ids})

    assert error, "збій запису мусить повертати помилку"
    with Session(engine) as db:
        assert all(db.get(Order, oid).sum3d_pending for oid in ids), (
            "після збою позначки зникли — фоновий повтор такі роботи вже не знайде"
        )


def test_edits_of_different_days_go_as_separate_batches(monkeypatch):
    """Звірка позицій і запис живуть у межах ОДНІЄЇ вкладки."""
    engine = _engine()
    with Session(engine, expire_on_commit=False) as db:
        today = _orders(db, 2, tab="17.09.26")
        yesterday = _orders(db, 2, tab="16.09.26")
    calls = _wire(monkeypatch, engine)

    wb.write_fields_bulk({oid: ({"sum3d_id"}, set()) for oid in today + yesterday})

    assert calls["resolve"] == 2
    assert calls["write"] == 2


def test_the_queue_coalesces_edits_into_one_batch(monkeypatch):
    """Кілька правок підряд мусять зійтись в ОДИН запис.

    Саме заради цього вікно коалесценції й існує: інакше пачка виродилась би в
    ту саму чергу, тільки без очікування.
    """
    seen: list[dict] = []
    monkeypatch.setattr(wb, "write_fields_bulk", lambda batch: seen.append(dict(batch)))

    for order_id in range(1, 6):
        wb.queue_sheet_fields(order_id, {"sum3d_id"})
    wb.flush_field_batch_now(timeout=10)

    assert len(seen) == 1, f"замість однієї пачки вийшло {len(seen)}"
    assert sorted(seen[0]) == [1, 2, 3, 4, 5]
