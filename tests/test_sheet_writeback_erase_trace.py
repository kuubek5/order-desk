"""Стирання рядка лишає слід ДАНИМИ, з якого росте «Відновити рядок» (B.8).

Це єдине місце, де `erased_row`/`erased_values` заповнюються. Якщо воно
мовчки перестане їх писати, кнопка просто не зʼявиться — тобто збій, який
нічого не ламає видимо й помічається аж тоді, коли рядок треба повернути.
"""

import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order, SyncLog
from app.services import sheet_writeback as wb


@pytest.fixture
def db_factory(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(wb, "writeback_session", factory)
    # Воркер має відпрацювати ТУТ, а не в пулі: тест перевіряє його результат.
    monkeypatch.setattr(wb, "submit_sheet_write", lambda fn: fn())
    monkeypatch.setattr(wb, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(wb, "get_worksheet_by_name", lambda ss, name: MagicMock())
    return factory


def _order(db: Session) -> Order:
    order = Order(
        source="lab", work_order_no="24122", sheet_tab="07.09.26", row_number=7,
        status="нове",
    )
    db.add(order)
    db.commit()
    return order


def test_erased_content_is_stored_as_data_not_only_text(db_factory, monkeypatch):
    with db_factory() as db:
        order = _order(db)
        order_id = order.id

    monkeypatch.setattr(wb, "clear_order_row", lambda ws, o: True)
    monkeypatch.setattr(
        wb, "take_last_erased",
        lambda oid: (13, ["1", "24122", "2", "моно а3", "анатомія"]),
    )

    wb.clear_sheet_row_background(order_id)

    with db_factory() as db:
        entry = db.query(SyncLog).one()
    assert entry.erased_row == 13
    assert json.loads(entry.erased_values)[1] == "24122"
    assert entry.erased_restored_at is None
    # Текст лишається для читання людиною.
    assert "24122" in entry.message


def test_empty_row_content_leaves_nothing_to_restore(db_factory, monkeypatch):
    """Порожній рядок відновлювати нема сенсу — кнопки бути не повинно."""
    with db_factory() as db:
        order = _order(db)
        order_id = order.id

    monkeypatch.setattr(wb, "clear_order_row", lambda ws, o: True)
    monkeypatch.setattr(wb, "take_last_erased", lambda oid: (13, ["", "  "]))

    wb.clear_sheet_row_background(order_id)

    with db_factory() as db:
        entry = db.query(SyncLog).one()
    assert entry.erased_row is None and entry.erased_values is None


def test_guard_block_is_logged_with_its_own_reason(db_factory, monkeypatch):
    """Запобіжник ≠ «рядок не підтверджено»: інакше причину не відрізнити."""
    from app.sheet_erase_guard import SheetEraseBlocked

    with db_factory() as db:
        order = _order(db)
        order_id = order.id

    def blocked(ws, o):
        raise SheetEraseBlocked(20, 20)

    monkeypatch.setattr(wb, "clear_order_row", blocked)

    wb.clear_sheet_row_background(order_id)

    with db_factory() as db:
        entry = db.query(SyncLog).one()
    assert entry.status == "error"
    assert "стирання зупинено" in entry.message
    assert entry.erased_row is None
