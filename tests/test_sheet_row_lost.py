"""Синк помічає роботу, чийого рядка в таблиці немає (22.10.26, #6472).

Рядок поштової роботи видалили свідомо, на його номер ліг інший лист. Синк
поштові роботи не архівує (і правильно — рішення за людиною), тож доти
фоновий повтор заливки молотив вічно. Тепер: ключ роботи не трапляється в
цілком прочитаній вкладці (або її рядок зведено з іншою роботою)
`ROWLESS_PASSES` проходів поспіль → `sheet_row_lost_at`; повтор її оминає,
банер показує ОДИН запис із діями. Синк сам нічого не архівує. Імена вигадані.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import sync as sync_module
from app.models import Order, SyncLog
from app.services import sheet_stuck_writes as stuck
from app.services import sheet_writeback
from app.sync import ROWLESS_PASSES, sync_tab
from tests.test_sync import make_client_row, make_session

TAB = "20.10.26"


@pytest.fixture(autouse=True)
def _clean():
    sync_module._rowless_streak.clear()
    stuck.reset()
    sheet_writeback._pending_fill_attempts.clear()
    sheet_writeback._pending_sum3d_attempts.clear()
    yield
    sync_module._rowless_streak.clear()
    stuck.reset()


def _old() -> datetime:
    return datetime.utcnow() - timedelta(minutes=10)


def _mail(session, *, row, client="Клієнт-А", material="mono a2", qty="2", **kw) -> Order:
    order = Order(
        source="email", sheet_tab=TAB, row_number=row, client_name=client,
        material_color=material, quantity=qty, status="знайдено при видачі",
        created_at=_old(), **kw,
    )
    session.add(order)
    session.commit()
    return order


def _row(row, client, material, qty):
    return make_client_row(row_number=row, kind=client, material_color=material, quantity=qty,
                           sum3d_id="", calculated="")


def _passes(session, rows, n):
    for _ in range(n):
        sync_tab(session, TAB, rows, raw_row_count=len(rows) + 6)
        session.commit()


def test_incident_two_works_on_one_row_marks_only_the_one_the_row_lost():
    """#6472 і #6480 обидві кажуть «мій рядок 74»; рядок належить #6480."""
    with make_session() as session:
        lost = _mail(session, row=74, fill_pending="clear")
        holder = _mail(session, row=74, client="Лаб-Б", material="mono a3", qty="1")
        rows = [_row(74, "Лаб-Б", "mono a3", "1"), _row(70, "Клієнт-А", "mono a2", "1")]

        _passes(session, rows, ROWLESS_PASSES - 1)
        assert lost.sheet_row_lost_at is None, "двох проходів замало — читання могло бути поганим"

        _passes(session, rows, 1)
        assert lost.sheet_row_lost_at is not None
        assert holder.sheet_row_lost_at is None
        assert lost.archived_at is None, "синк сам НЕ архівує — рішення за людиною"
        logs = [log.message for log in session.scalars(select(SyncLog))]
        assert any(f"order {lost.id}: рядка" in m for m in logs)

        # Ще проходи — без нових записів у журнал.
        _passes(session, rows, 2)
        assert sum(f"order {lost.id}: рядка" in m for m in
                   (log.message for log in session.scalars(select(SyncLog)))) == 1


def test_row_back_clears_the_mark():
    with make_session() as session:
        order = _mail(session, row=74)
        _passes(session, [_row(70, "Інший", "pmma", "1")], ROWLESS_PASSES)
        assert order.sheet_row_lost_at is not None

        _passes(session, [_row(70, "Інший", "pmma", "1"), _row(74, "Клієнт-А", "mono a2", "2")], 1)
        assert order.sheet_row_lost_at is None
        assert order.id not in sync_module._rowless_streak


def test_a_gap_in_the_streak_starts_over():
    with make_session() as session:
        order = _mail(session, row=74)
        without = [_row(70, "Інший", "pmma", "1")]
        with_row = without + [_row(74, "Клієнт-А", "mono a2", "2")]
        _passes(session, without, ROWLESS_PASSES - 1)
        _passes(session, with_row, 1)
        _passes(session, without, ROWLESS_PASSES - 1)
        assert order.sheet_row_lost_at is None


def test_empty_read_never_counts():
    """Обірване читання (лише заголовки або нічого) — не «рядка немає»."""
    with make_session() as session:
        order = _mail(session, row=74)
        for _ in range(ROWLESS_PASSES + 1):
            sync_tab(session, TAB, [], raw_row_count=0)
            session.commit()
        assert order.sheet_row_lost_at is None


def test_many_at_once_is_a_bad_read_not_people():
    with make_session() as session:
        works = [_mail(session, row=60 + i, client=f"К{i}") for i in range(8)]
        _passes(session, [_row(90, "Інший", "pmma", "1")], ROWLESS_PASSES + 1)
        assert all(w.sheet_row_lost_at is None for w in works)


def test_nameless_twin_is_unknown_not_lost():
    """Безіменна робота, чий ключ (матеріал + к-сть) у вкладці є, — «не знаю»."""
    with make_session() as session:
        first = _mail(session, row=74, client="", material="pmma a2", qty="6")
        second = _mail(session, row=74, client="", material="pmma a2", qty="6")
        _passes(session, [_row(74, "", "pmma a2", "6")], ROWLESS_PASSES + 1)
        assert first.sheet_row_lost_at is None and second.sheet_row_lost_at is None


def test_fresh_work_is_not_judged_by_an_older_read():
    with make_session() as session:
        order = _mail(session, row=74)
        order.created_at = datetime.utcnow() + timedelta(minutes=1)
        session.commit()
        for _ in range(ROWLESS_PASSES + 1):
            sync_tab(session, TAB, [_row(70, "Інший", "pmma", "1")], raw_row_count=7,
                     rows_read_at=datetime.utcnow())
            session.commit()
        assert order.sheet_row_lost_at is None


def test_sheet_client_still_goes_to_archive_by_reconciliation():
    """Вписаний клієнт без рядка — як і раніше, архів реконсиляцією (позначка
    «без рядка» його не стосується)."""
    with make_session() as session:
        order = Order(source="sheet_client", sheet_tab=TAB, row_number=74, client_name="К",
                      material_color="mono a2", quantity="2", status="нове", created_at=_old())
        session.add(order)
        session.commit()
        _passes(session, [_row(70, "Інший", "pmma", "1")], ROWLESS_PASSES)
        assert order.archived_at is not None
        assert order.sheet_row_lost_at is None


def test_retries_skip_lost_works(monkeypatch):
    with make_session() as session:
        lost = _mail(session, row=74, fill_pending="clear", sum3d_id="15-35-56",
                     sum3d_pending="15-35-56", sheet_row_lost_at=datetime.now())
        fine = _mail(session, row=75, client="Б", fill_pending="clear", sum3d_id="1",
                     sum3d_pending="1")
        filled: list[int] = []
        written: list[int] = []
        monkeypatch.setattr(sheet_writeback, "set_client_row_fill_background",
                            lambda order_id, blue: filled.append(order_id))
        monkeypatch.setattr(sheet_writeback, "submit_sheet_write",
                            lambda fn, order_id, *a: written.append(order_id))
        monkeypatch.setattr("app.sheets.quota_is_tight", lambda: False)

        sheet_writeback.retry_pending_fills(session, now=1000.0)
        sheet_writeback.retry_pending_sum3d(session, now=1000.0)
        assert filled == [fine.id] and written == [fine.id]

        futures = sheet_writeback.retry_failed_writes_now(session, {lost.id}, {lost.id}, now=2000.0)
        assert futures == [] and filled == [fine.id]


def test_banner_shows_a_lost_work_once_with_what_did_not_arrive():
    with make_session() as session:
        lost = _mail(session, row=74, fill_pending="clear", sum3d_id="15-35-56",
                     sum3d_pending="15-35-56", sheet_row_lost_at=datetime(2026, 10, 21, 5, 54))
        quiet = _mail(session, row=75, client="Б", sheet_row_lost_at=datetime(2026, 10, 21, 6, 0))
        stuck.note_write_failed("fill", lost, "рядок у таблиці не підтверджено")

        stuck.observe(session, now=1000.0)
        items = stuck.failed_sheet_writes()
        by_id = {item.order_id: item for item in items}
        assert len([i for i in items if i.order_id == lost.id]) == 1
        assert by_id[lost.id].reason == "lost"
        assert set(by_id[lost.id].pending) == {"fill", "sum3d"}
        assert by_id[quiet.id].reason == "lost" and by_id[quiet.id].pending == ()

        stuck.forget_order(lost.id, kinds=("fill",))
        assert {i.order_id: i for i in stuck.failed_sheet_writes()}[lost.id].pending == ("sum3d",)
        stuck.forget_order(lost.id)
        assert lost.id not in {i.order_id for i in stuck.failed_sheet_writes()}
