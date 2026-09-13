"""Заливка рядка, яка не доїхала в таблицю, більше не губиться мовчки.

Оператор ставить на видачі галочку «знайдено» — портал знімає синю заливку
рядка в таблиці, і саме знята заливка, а не статус у базі, є сигналом видачі
для всієї лабораторії (CLAUDE.md §2). Доти невдалий запис був НІМИЙ: рядок у
лог-файлі, жодного повтору. Обрив мережі → у таблиці назавжди лишалось синє,
тобто «не видано» для логістів, тоді як оператор був упевнений, що видав.

Тут перевіряється весь ланцюг: позначка ставиться при збої, знімається при
успіху, повтор її підбирає із запобіжниками, а синк знімає її, щойно таблиця
показала потрібний колір.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order
from app.services import sheet_writeback


@pytest.fixture(autouse=True)
def _fresh_attempts():
    """Словник спроб живе в памʼяті ПРОЦЕСУ, тож між тестами він протікає:
    дросель «раз на 2 хв» тихо гасив би виклики наступного тесту."""
    sheet_writeback._pending_fill_attempts.clear()
    yield
    sheet_writeback._pending_fill_attempts.clear()


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _order(db, **kw):
    d = dict(source="sheet_client", sheet_tab="26.08.26", row_number=54,
             client_name="Басараб", status="знайдено при видачі")
    d.update(kw)
    o = Order(**d)
    db.add(o)
    db.commit()
    return o


def _run_fill(db, order, *, blue, error):
    """Прогнати фонового воркера заливки синхронно, з підміненим записом."""
    captured = {}

    def fake_submit(fn, *a, **kw):
        captured["ran"] = True
        return fn(*a, **kw)

    with patch.object(sheet_writeback, "submit_sheet_write", fake_submit), \
         patch.object(sheet_writeback, "writeback_session", lambda: _session_cm(db)), \
         patch.object(sheet_writeback, "set_client_row_fill", lambda *a, **k: error):
        sheet_writeback.set_client_row_fill_background(order.id, blue=blue)
    assert captured.get("ran")


class _session_cm:
    """Підсунути воркеру ТУ САМУ сесію тесту, не закриваючи її."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        return False


# --- позначка ставиться й знімається -----------------------------------------


def test_failed_clear_leaves_a_mark():
    """Найдорожчий випадок: галочка «знайдено», а синє в таблиці лишилось."""
    with Session(_database(), expire_on_commit=False) as db:
        order = _order(db)
        _run_fill(db, order, blue=False, error="мережа впала")
        assert order.fill_pending == "clear"


def test_failed_repaint_leaves_a_mark():
    with Session(_database(), expire_on_commit=False) as db:
        order = _order(db, status="нове")
        _run_fill(db, order, blue=True, error="мережа впала")
        assert order.fill_pending == "blue"


def test_success_clears_the_mark():
    with Session(_database(), expire_on_commit=False) as db:
        order = _order(db, fill_pending="clear")
        _run_fill(db, order, blue=False, error=None)
        assert order.fill_pending is None


def test_a_later_action_overwrites_the_stale_wish():
    """Оператор зняв галочку, поки заливка не доїхала — повтор мусить поставити
    СИНЄ, а не застаріле «зняти». Тому зберігаємо бажаний колір, не прапорець."""
    with Session(_database(), expire_on_commit=False) as db:
        order = _order(db)
        _run_fill(db, order, blue=False, error="мережа впала")
        assert order.fill_pending == "clear"
        _run_fill(db, order, blue=True, error="мережа впала")
        assert order.fill_pending == "blue"


# --- повтор ------------------------------------------------------------------


def test_retry_picks_up_pending_rows():
    with Session(_database(), expire_on_commit=False) as db:
        order = _order(db, fill_pending="clear")
        calls = []
        with patch.object(sheet_writeback, "set_client_row_fill_background",
                          lambda oid, *, blue: calls.append((oid, blue))), \
             patch.object(sheet_writeback.sync_control, "is_paused", lambda: False), \
             patch("app.sheets.quota_is_tight", lambda: False):
            assert sheet_writeback.retry_pending_fills(db, now=1000.0) == 1
        assert calls == [(order.id, False)]


def test_retry_asks_for_blue_when_that_is_what_is_missing():
    with Session(_database(), expire_on_commit=False) as db:
        order = _order(db, fill_pending="blue")
        calls = []
        with patch.object(sheet_writeback, "set_client_row_fill_background",
                          lambda oid, *, blue: calls.append((oid, blue))), \
             patch.object(sheet_writeback.sync_control, "is_paused", lambda: False), \
             patch("app.sheets.quota_is_tight", lambda: False):
            sheet_writeback.retry_pending_fills(db, now=1000.0)
        assert calls == [(order.id, True)]


def test_retry_does_not_hammer_the_same_row():
    """Не частіше ніж раз на 2 хв на роботу — інакше тік синку кожні 15 с
    перетворив би повтор на обстріл таблиці."""
    with Session(_database(), expire_on_commit=False) as db:
        _order(db, fill_pending="clear")
        calls = []
        with patch.object(sheet_writeback, "set_client_row_fill_background",
                          lambda oid, *, blue: calls.append(oid)), \
             patch.object(sheet_writeback.sync_control, "is_paused", lambda: False), \
             patch("app.sheets.quota_is_tight", lambda: False):
            sheet_writeback.retry_pending_fills(db, now=1000.0)
            sheet_writeback.retry_pending_fills(db, now=1030.0)   # рано
            sheet_writeback.retry_pending_fills(db, now=1200.0)   # можна
        assert len(calls) == 2


def test_retry_stands_down_on_pause_and_tight_quota():
    for paused, tight in ((True, False), (False, True)):
        with Session(_database(), expire_on_commit=False) as db:
            _order(db, fill_pending="clear")
            sheet_writeback._pending_fill_attempts.clear()
            with patch.object(sheet_writeback.sync_control, "is_paused", lambda: paused), \
                 patch("app.sheets.quota_is_tight", lambda: tight):
                assert sheet_writeback.retry_pending_fills(db, now=1000.0) == 0


def test_archived_work_is_not_retried():
    from app.business_day import utc_now

    with Session(_database(), expire_on_commit=False) as db:
        _order(db, fill_pending="clear", archived_at=utc_now())
        with patch.object(sheet_writeback.sync_control, "is_paused", lambda: False), \
             patch("app.sheets.quota_is_tight", lambda: False):
            assert sheet_writeback.retry_pending_fills(db, now=1000.0) == 0


def test_retry_batch_is_capped():
    """Пул запису один на всі правки — пачка повторів не має стати перед
    живими діями оператора."""
    with Session(_database(), expire_on_commit=False) as db:
        for _ in range(sheet_writeback.PENDING_FILL_RETRY_BATCH + 3):
            _order(db, fill_pending="clear")

        with patch.object(sheet_writeback, "set_client_row_fill_background",
                          lambda oid, *, blue: None), \
             patch.object(sheet_writeback.sync_control, "is_paused", lambda: False), \
             patch("app.sheets.quota_is_tight", lambda: False):
            n = sheet_writeback.retry_pending_fills(db, now=1000.0)
        assert n == sheet_writeback.PENDING_FILL_RETRY_BATCH


# --- синк знімає позначку -----------------------------------------------------


def _agrees(fill, wanted):
    """Та сама умова, що в app/sync.py: таблиця показує бажаний стан."""
    return (fill in ("blue", "grey")) == (wanted == "blue")


def test_sync_clears_the_mark_once_the_sheet_agrees():
    assert _agrees("", "clear")          # зняли — цього й хотіли
    assert _agrees("blue", "blue")       # повернули синє — теж
    assert not _agrees("blue", "clear")  # синє ще стоїть — тримаємо позначку
    assert not _agrees("", "blue")


def test_grey_counts_as_agreement_for_blue():
    """Сіре — власна позначка лабораторії й теж означає «не видано». Інакше
    повтор затирав би чужий сірий мазок своїм синім щодві хвилини."""
    assert _agrees("grey", "blue")
    assert not _agrees("grey", "clear")


def test_the_row_template_shows_the_mark():
    """Значок мусить бути саме в СПІЛЬНОМУ рядку: його малюють два режими
    списку, і друга копія розійшлася б на першій правці (CLAUDE.md §14)."""
    from pathlib import Path

    row = Path("app/templates/_handout_work_row.html").read_text(encoding="utf-8")
    assert "order.fill_pending" in row
    assert "sync-warning" in row


def test_unrelated_orders_are_left_alone():
    """Позначка — тільки про клієнтські рядки таблиці."""
    with Session(_database(), expire_on_commit=False) as db:
        lab = _order(db, source="lab", client_name=None, work_order_no="24122")
        _run_fill(db, lab, blue=False, error=None)
        assert lab.fill_pending is None


def test_order_defaults_to_no_mark():
    with Session(_database(), expire_on_commit=False) as db:
        assert _order(db).fill_pending is None


def test_group_failure_marks_every_row():
    """Групова галочка падає цілою пачкою — тоді синього в таблиці лишається
    найбільше, і саме тут мовчання коштувало б найдорожче."""
    with Session(_database(), expire_on_commit=False) as db:
        a, b = _order(db), _order(db)
        # Своя сесія — саме в цьому суть функції: основна вже відкотилась.
        with patch.object(sheet_writeback, "writeback_session", lambda: _session_cm(db)):
            sheet_writeback._remember_failed_fills([a.id, b.id], "clear")
        assert db.get(Order, a.id).fill_pending == "clear"
        assert db.get(Order, b.id).fill_pending == "clear"


def test_retry_is_wired_into_the_sync_tick():
    """Без цього виклику позначка ставилась би, а ніхто б її не підбирав."""
    from pathlib import Path

    web = Path("app/web.py").read_text(encoding="utf-8")
    assert "_retry_pending_fills_tick(db)" in web
    assert "retry_pending_fills" in web


def test_retry_forgets_rows_that_are_no_longer_pending():
    """Словник спроб живе в пам'яті процесу — він не має рости вічно."""
    with Session(_database(), expire_on_commit=False) as db:
        order = _order(db, fill_pending="clear")
        with patch.object(sheet_writeback, "set_client_row_fill_background",
                          lambda oid, *, blue: None), \
             patch.object(sheet_writeback.sync_control, "is_paused", lambda: False), \
             patch("app.sheets.quota_is_tight", lambda: False):
            sheet_writeback.retry_pending_fills(db, now=1000.0)
            assert order.id in sheet_writeback._pending_fill_attempts
            order.fill_pending = None
            db.commit()
            sheet_writeback.retry_pending_fills(db, now=2000.0)
        assert order.id not in sheet_writeback._pending_fill_attempts


