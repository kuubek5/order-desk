"""Запис у таблицю, що не дійшов, — сказати оператору одразу, у CRM.

30.09.26 власник: «стовідсоткова гарантія, що записалося, а якщо ні — щоб нам
про це повідомило». 01.10.26: сигнал НЕ в Telegram, а в самій CRM, одразу при
збої, зі списком робіт, причиною й кнопкою «Записати зараз».

Стережемо ПЕРЕХОДИ, а не стани:
* свіжа позначка без збою (звичайний запис, 1–2 с) — тиша;
* місце запису сказало «не дійшло» — у списку одразу, з причиною;
* дописалось (місце запису або синк зняв позначку) — зі списку геть;
* позначка довше 5 хв без збою (після рестарту) — у списку «причина невідома»,
  один запис у журнал на епізод, один «дописано»;
* пауза синку — тиша (записи не йдуть свідомо);
* позначка старого ID (не дорівнює поточному) — не рахується, як і в рядку;
* Telegram — більше нічого не шле.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import Order, SyncLog
from app.services import sheet_stuck_writes as stuck
from app.services import sheet_writeback
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура
from tests.test_sync import make_session

LATER = stuck.STUCK_AFTER_SECONDS + 1
GRACE = stuck._FAILURE_GRACE_SECONDS + 1


@pytest.fixture(autouse=True)
def _clean_state():
    stuck.reset()
    sheet_writeback._pending_sum3d_attempts.clear()
    sheet_writeback._pending_fill_attempts.clear()
    yield
    stuck.reset()
    sheet_writeback._pending_sum3d_attempts.clear()
    sheet_writeback._pending_fill_attempts.clear()


@pytest.fixture
def telegram(monkeypatch):
    """Бот увімкнено — щоб довести, що сторож туди більше нічого не ставить."""
    from app.services import telegram_bot

    queued: list[dict] = []
    monkeypatch.setattr(telegram_bot, "bot_enabled", lambda db: True)
    monkeypatch.setattr(telegram_bot, "enqueue", lambda db, **kw: queued.append(kw) or True)
    return queued


def _order(session, **fields) -> Order:
    base = dict(source="lab", sheet_tab="30.09.26", row_number=1, work_order_no="33378")
    base.update(fields)
    order = Order(**base)
    session.add(order)
    session.commit()
    return order


def _logs(session) -> list[SyncLog]:
    return list(session.scalars(select(SyncLog).order_by(SyncLog.id)))


# --- реєстр збоїв -------------------------------------------------------------


def test_fresh_pending_mark_without_failure_is_silent(telegram):
    with make_session() as session:
        _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        assert stuck.observe(session, now=0.0) is None
        assert stuck.observe(session, now=LATER - 2) is None
        assert stuck.failed_sheet_writes() == []
        assert _logs(session) == []
        assert telegram == []


def test_failure_shows_at_once_with_reason_and_goes_on_success():
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21",
                       client_name="Голій")
        stuck.note_write_failed("sum3d", order, "рядок у таблиці не підтверджено",
                                now=0.0, wall=datetime(2026, 10, 1, 10, 42))
        [item] = stuck.failed_sheet_writes()
        assert (item.kind, item.order_id, item.who, item.tab) == ("sum3d", order.id, "Голій", "30.09.26")
        assert item.reason == "row" and "виправте рядок" in item.reason_text
        assert item.since == datetime(2026, 10, 1, 10, 42)

        # Тік синку з живою позначкою — збій лишається.
        stuck.observe(session, now=GRACE)
        assert len(stuck.failed_sheet_writes()) == 1

        stuck.note_write_ok("sum3d", order.id)
        assert stuck.failed_sheet_writes() == []


def test_repeated_failure_keeps_first_time_and_takes_fresh_reason():
    with make_session() as session:
        order = _order(session, fill_pending="clear", source="sheet_client", client_name="Басараб")
        stuck.note_write_failed("fill", order, "Connection reset", now=0.0,
                                wall=datetime(2026, 10, 1, 9, 0))
        stuck.note_write_failed("fill", order, "рядок у таблиці не підтверджено", now=120.0,
                                wall=datetime(2026, 10, 1, 9, 2))
        [item] = stuck.failed_sheet_writes()
        assert item.since == datetime(2026, 10, 1, 9, 0)
        assert item.reason == "row"


def test_sync_clearing_the_mark_drops_the_failure_after_grace():
    """Синк побачив значення в таблиці й зняв позначку (app/sync.py) — збій
    сам зникає. Але не з першого ж тіку: місце запису комітить позначку ПІСЛЯ
    повідомлення, і тік між ними стер би свіжий збій."""
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.note_write_failed("sum3d", order, "timeout", now=100.0)
        order.sum3d_pending = None
        session.commit()
        stuck.observe(session, now=101.0)
        assert len(stuck.failed_sheet_writes()) == 1
        stuck.observe(session, now=100.0 + GRACE)
        assert stuck.failed_sheet_writes() == []


def test_classify_reasons():
    assert stuck.classify("рядок у таблиці не підтверджено — не записано") == "row"
    assert stuck.classify("вкладку '30.09.26' не знайдено") == "tab"
    assert stuck.classify("HTTPSConnectionPool: Read timed out") == "net"
    assert stuck.classify(None) == "net"


def test_failure_on_pause_is_not_recorded(monkeypatch):
    from app import sync_control

    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        monkeypatch.setattr(sync_control, "is_paused", lambda: True)
        stuck.note_write_failed("sum3d", order, "timeout")
        assert stuck.failed_sheet_writes() == []


# --- без причини: позначка висить довше 5 хв (рестарт) -------------------------


def test_stuck_mark_without_failure_lists_as_unknown_and_logs_one_episode(telegram):
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        _order(session, sum3d_id="18-00-00", fill_pending="blue", source="sheet_client",
               client_name="Басараб")

        stuck.observe(session, now=0.0)
        snap = stuck.observe(session, now=LATER)
        assert snap is not None and (snap.sum3d, snap.fill) == (1, 1)
        items = stuck.failed_sheet_writes()
        assert {(i.kind, i.reason) for i in items} == {("sum3d", "unknown"), ("fill", "unknown")}

        logs = _logs(session)
        assert [log.status for log in logs] == ["warning"]
        assert "Sum3D — 1" in logs[0].message and "синя заливка — 1" in logs[0].message
        assert "33378 (30.09.26)" in logs[0].message

        # Той самий епізод — без повторів у журнал.
        stuck.observe(session, now=LATER + 60)
        assert len(_logs(session)) == 1

        # Дописалось: позначки зняті — список порожній, один запис «дописано».
        order.sum3d_pending = None
        for other in session.scalars(select(Order).where(Order.fill_pending.is_not(None))):
            other.fill_pending = None
        session.commit()
        assert stuck.observe(session, now=LATER + 180) is None
        assert stuck.failed_sheet_writes() == []
        assert [log.status for log in _logs(session)] == ["warning", "ok"]
        assert telegram == []  # Telegram прибрано (власник 01.10.26)


def test_known_failure_is_not_duplicated_as_unknown():
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        stuck.note_write_failed("sum3d", order, "timeout", now=LATER)
        stuck.observe(session, now=LATER)
        [item] = stuck.failed_sheet_writes()
        assert item.reason == "net"


def test_a_new_episode_after_recovery_logs_again():
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        stuck.observe(session, now=LATER)
        order.sum3d_pending = None
        session.commit()
        stuck.observe(session, now=LATER + 10)

        order.sum3d_pending = "17-05-21"
        session.commit()
        stuck.observe(session, now=LATER + 20)
        stuck.observe(session, now=LATER + 20 + LATER)
        assert [log.status for log in _logs(session)] == ["warning", "ok", "warning"]


def test_stale_pending_of_an_old_id_is_not_counted():
    """Позначка не дорівнює поточному ID — повтор її не пише, рядок не показує;
    тут вона теж не рахується (той самий предикат)."""
    with make_session() as session:
        _order(session, sum3d_id="18-00-00", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        assert stuck.observe(session, now=LATER) is None
        assert stuck.failed_sheet_writes() == []


def test_archived_work_is_not_counted():
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21",
                       archived_at=datetime(2026, 9, 30))
        stuck.note_write_failed("sum3d", order, "timeout", now=0.0)
        stuck.observe(session, now=GRACE)
        assert stuck.observe(session, now=LATER) is None
        assert stuck.failed_sheet_writes() == []


def test_paused_sync_is_silent_and_restarts_the_clock(monkeypatch):
    from app import sync_control

    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        stuck.note_write_failed("sum3d", order, "timeout", now=0.0)
        monkeypatch.setattr(sync_control, "is_paused", lambda: True)
        assert stuck.observe(session, now=LATER) is None
        assert stuck.failed_sheet_writes() == []
        monkeypatch.setattr(sync_control, "is_paused", lambda: False)
        # Після паузи відлік заново: повтори теж почались заново.
        assert stuck.observe(session, now=LATER + 1) is None
        assert stuck.failed_sheet_writes() == []
        assert _logs(session) == []


def test_sync_worker_pause_branch_clears_the_list(monkeypatch):
    """Справжній шлях паузи: воркер синку на паузі робить `continue` ДО тіку
    сторожа, тож скидання мусить стояти в самій гілці паузи."""
    import app.web as web
    from app import sync_control

    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.note_write_failed("sum3d", order, "timeout")
    assert stuck.failed_sheet_writes()

    class OneTick:
        """Перше очікування (стартова затримка) — далі; друге — стоп."""
        calls = 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

        def is_set(self):
            return self.calls >= 2

    monkeypatch.setattr(sync_control, "is_paused", lambda: True)
    monkeypatch.setattr(web, "_record_sync_heartbeat", lambda *a, **k: None)
    web._sheet_sync_worker(OneTick())

    assert stuck.failed_sheet_writes() == []


def test_watchdog_no_longer_talks_to_telegram():
    from pathlib import Path

    source = Path("app/services/sheet_stuck_writes.py").read_text(encoding="utf-8")
    assert "telegram" not in source.lower().replace("не в telegram", "")


# --- місця запису повідомляють ------------------------------------------------


@contextmanager
def _same_session(db):
    yield db


def _bulk(db, batch, *, rows=None, tab_ok=True, write_exc=None):
    """Прогнати `write_fields_bulk` з підміненою таблицею."""
    def fake_tab(_ss, _name):
        return object() if tab_ok else None

    def fake_write(_ws, _plan):
        if write_exc:
            raise write_exc

    with patch.object(sheet_writeback, "writeback_session", lambda: _same_session(db)), \
         patch.object(sheet_writeback, "open_spreadsheet", lambda db=None: object()), \
         patch.object(sheet_writeback, "get_worksheet_by_name", fake_tab), \
         patch.object(sheet_writeback, "resolve_rows_bulk", lambda _ws, orders: rows or {}), \
         patch.object(sheet_writeback, "write_order_fields_bulk", fake_write):
        return sheet_writeback.write_fields_bulk(batch)


def test_bulk_unconfirmed_row_reports_row_reason_then_success_clears():
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        _bulk(session, {order.id: ({"sum3d_id"}, set())}, rows={})
        [item] = stuck.failed_sheet_writes()
        assert item.reason == "row"
        assert session.get(Order, order.id).sum3d_pending == "17-05-21"

        _bulk(session, {order.id: ({"sum3d_id"}, set())}, rows={order.id: 7})
        assert session.get(Order, order.id).sum3d_pending is None
        assert stuck.failed_sheet_writes() == []


def test_bulk_network_and_missing_tab_reasons():
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        _bulk(session, {order.id: ({"sum3d_id"}, set())}, rows={order.id: 7},
              write_exc=RuntimeError("Read timed out"))
        assert stuck.failed_sheet_writes()[0].reason == "net"
        _bulk(session, {order.id: ({"sum3d_id"}, set())}, tab_ok=False)
        assert stuck.failed_sheet_writes()[0].reason == "tab"


def test_bulk_failure_of_other_fields_is_not_a_signal():
    """Поле без позначки (оператор, коментар) повтор не пише — і в списку
    «не дійшло» йому робити нічого: кнопка однаково його не допише."""
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21")
        _bulk(session, {order.id: ({"operator"}, set())}, rows={})
        assert stuck.failed_sheet_writes() == []


def test_write_sheet_fields_reports_both_ways():
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21")
        with patch.object(sheet_writeback, "_write_sheet_fields", lambda *a: "Read timed out"):
            sheet_writeback.write_sheet_fields(session, order, {"sum3d_id"})
        assert order.sum3d_pending == "17-05-21"
        assert [i.reason for i in stuck.failed_sheet_writes()] == ["net"]
        with patch.object(sheet_writeback, "_write_sheet_fields", lambda *a: None):
            sheet_writeback.write_sheet_fields(session, order, {"sum3d_id"})
        assert order.sum3d_pending is None
        assert stuck.failed_sheet_writes() == []


def test_failed_sum3d_clear_does_not_hide_an_earlier_failure():
    """Очищення ID, що не дійшло, позначки не ставить, але й «дійшло» не є:
    збій у списку лишається (рецензія 01.10.26)."""
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.note_write_failed("sum3d", order, "timeout")
        order.sum3d_id = None
        with patch.object(sheet_writeback, "_write_sheet_fields", lambda *a: "timeout"):
            sheet_writeback.write_sheet_fields(session, order, {"sum3d_id"})
        assert len(stuck.failed_sheet_writes()) == 1


def test_fill_worker_reports_both_ways():
    with make_session() as session:
        order = _order(session, source="sheet_client", client_name="Басараб", row_number=54)

        def run(error):
            with patch.object(sheet_writeback, "submit_sheet_write", lambda fn, *a, **k: fn(*a, **k)), \
                 patch.object(sheet_writeback, "writeback_session", lambda: _same_session(session)), \
                 patch.object(sheet_writeback, "set_client_row_fill", lambda *a, **k: error):
                sheet_writeback.set_client_row_fill_background(order.id, blue=False)

        run("рядок у таблиці не підтверджено — заливку не змінено")
        [item] = stuck.failed_sheet_writes()
        assert (item.kind, item.reason, item.who) == ("fill", "row", "Басараб")
        run(None)
        assert stuck.failed_sheet_writes() == []


def test_group_clear_reports_unconfirmed_rows():
    with make_session() as session:
        a = _order(session, source="sheet_client", client_name="Басараб", row_number=54)
        b = _order(session, source="sheet_client", client_name="Басараб", row_number=55)
        with patch.object(sheet_writeback, "submit_sheet_write", lambda fn, *x, **k: fn(*x, **k)), \
             patch.object(sheet_writeback, "writeback_session", lambda: _same_session(session)), \
             patch.object(sheet_writeback, "open_spreadsheet", lambda db=None: object()), \
             patch.object(sheet_writeback, "get_worksheet_by_name", lambda _s, _n: type("W", (), {"id": 1})()), \
             patch.object(sheet_writeback, "resolve_order_row",
                          lambda _w, o: 54 if o.id == a.id else None), \
             patch.object(sheet_writeback, "clear_row_fills", lambda *x: None):
            sheet_writeback.clear_group_fills_background([a.id, b.id])
        assert session.get(Order, b.id).fill_pending == "clear"
        assert [(i.order_id, i.reason) for i in stuck.failed_sheet_writes()] == [(b.id, "row")]


# --- «Записати зараз» ---------------------------------------------------------


def test_retry_now_ignores_the_throttle_and_batches_sum3d():
    with make_session() as session:
        a = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21", calculated_raw="Р")
        b = _order(session, sum3d_id="18-00-00", sum3d_pending="18-00-00")
        stale = _order(session, sum3d_id="19-00-00")   # уже дописано тим часом
        f = _order(session, source="sheet_client", client_name="Басараб", fill_pending="blue")
        sheet_writeback._pending_sum3d_attempts[a.id] = 999.0   # тротл щойно був
        submitted: list = []
        fills: list = []
        with patch.object(sheet_writeback, "submit_sheet_write",
                          lambda fn, *x, **k: submitted.append((fn, x)) or "future"), \
             patch.object(sheet_writeback, "set_client_row_fill_background",
                          lambda oid, *, blue: fills.append((oid, blue)) or "fill-future"):
            futures = sheet_writeback.retry_failed_writes_now(
                session, {a.id, b.id, stale.id}, {f.id}, now=1000.0,
            )
        assert futures == ["future", "fill-future"]
        [(fn, (batch,))] = submitted
        assert fn is sheet_writeback.write_fields_bulk
        assert batch == {a.id: ({"sum3d_id", "calculated_raw"}, set()), b.id: ({"sum3d_id"}, set())}
        assert fills == [(f.id, True)]
        # Тік синку одразу за кнопкою не поставить те саме вдруге.
        assert sheet_writeback._pending_sum3d_attempts[a.id] == 1000.0
        assert sheet_writeback._pending_fill_attempts[f.id] == 1000.0


def _client(app):
    client = MiniClient(app)
    client.login(*ADMIN)
    return client


def test_banner_on_queue_page_and_in_its_poll(app_db):  # noqa: F811
    """Справжній рендер: список із переходом до рядка й кнопкою — у сторінці
    черги й у фрагменті, який полл `#sheet-writes-slot` тягне кожні 15 с."""
    app, session_factory = app_db
    with session_factory() as db:
        order = _order(db, sum3d_id="17-05-21", sum3d_pending="17-05-21", client_name="Голій")
        stuck.note_write_failed("sum3d", order, "рядок у таблиці не підтверджено")

    client = _client(app)
    status, _, html = client.get("/")
    assert status == 200, html[:500]
    assert 'id="sheet-writes-slot"' in html
    assert "swf-banner" in html and "Голій" in html
    assert f"focus={order.id}" in html and "date=30.09.26" in html
    assert "виправте рядок у таблиці" in html
    assert 'hx-post="/sheets/write-failures/retry"' in html

    status, _, fragment = client.get("/sheets/write-failures")
    assert status == 200
    assert "swf-banner" in fragment and "Записати зараз" in fragment
    # Старий банер у слоті масового видалення не дублює новий.
    _, _, mv = client.get("/sheets/mass-vanish")
    assert "swf-banner" not in mv and "stuck-writes-banner" not in mv


def test_handout_page_has_the_slot(app_db):  # noqa: F811
    app, _ = app_db
    status, _, html = _client(app).get("/handout")
    assert status == 200, html[:500]
    assert 'id="sheet-writes-slot"' in html
    assert 'hx-get="/sheets/write-failures"' in html


def test_no_banner_when_nothing_failed(app_db):  # noqa: F811
    app, _ = app_db
    client = _client(app)
    _, _, html = client.get("/")
    assert "swf-banner" not in html
    _, _, fragment = client.get("/sheets/write-failures")
    assert "swf-banner" not in fragment


def test_retry_button_refuses_on_pause(app_db, monkeypatch):  # noqa: F811
    import json

    from app import sync_control

    app, session_factory = app_db
    with session_factory() as db:
        order = _order(db, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.note_write_failed("sum3d", order, "timeout")
    client = _client(app)
    called: list = []
    monkeypatch.setattr(sheet_writeback, "retry_failed_writes_now",
                        lambda *a, **k: called.append(a) or [])
    monkeypatch.setattr(sync_control, "is_paused", lambda: True)
    status, headers, _ = client.post("/sheets/write-failures/retry")
    assert status == 200
    toast = json.loads(headers["hx-trigger"])["toast"]
    assert toast["kind"] == "warning" and "паузі" in toast["message"]
    assert called == []


def test_retry_button_writes_and_reports(app_db, monkeypatch):  # noqa: F811
    """Повний шлях кнопки: повтор дописав (позначку знято, місце запису
    сказало «ок») — тост «записано», банер у відповіді порожній."""
    import json
    from concurrent.futures import Future

    app, session_factory = app_db
    with session_factory() as db:
        order = _order(db, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        order_id = order.id
        stuck.note_write_failed("sum3d", order, "timeout")

    def fake_retry(db, sum3d_ids, fill_ids, **_):
        assert sum3d_ids == {order_id} and fill_ids == set()
        with session_factory() as s:
            s.get(Order, order_id).sum3d_pending = None
            s.commit()
        stuck.note_write_ok("sum3d", order_id)
        done: Future = Future()
        done.set_result(None)
        return [done]

    monkeypatch.setattr(sheet_writeback, "retry_failed_writes_now", fake_retry)
    status, headers, body = _client(app).post("/sheets/write-failures/retry")
    assert status == 200
    toast = json.loads(headers["hx-trigger"])["toast"]
    assert toast["kind"] == "success" and "Записано в таблицю: 1" in toast["message"]
    assert "swf-banner" not in body


def test_retry_button_says_what_is_still_failing(app_db, monkeypatch):  # noqa: F811
    import json
    from concurrent.futures import Future

    app, session_factory = app_db
    with session_factory() as db:
        order = _order(db, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.note_write_failed("sum3d", order, "рядок у таблиці не підтверджено")

    def fake_retry(*_a, **_k):
        done: Future = Future()
        done.set_result(None)
        return [done]

    monkeypatch.setattr(sheet_writeback, "retry_failed_writes_now", fake_retry)
    status, headers, body = _client(app).post("/sheets/write-failures/retry")
    toast = json.loads(headers["hx-trigger"])["toast"]
    assert toast["kind"] == "error" and "виправте рядок" in toast["message"]
    assert "swf-banner" in body
