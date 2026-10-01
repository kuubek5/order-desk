"""Запис у таблицю, що застряг, — сказати людям (30.09.26).

Власник: «стовідсоткова гарантія, що записалося, а якщо ні — щоб нам про це
повідомило». База значення тримає, повтор іде сам, але єдиним сигналом довгого
збою був трикутник у рядку. Стережемо ПЕРЕХОДИ, а не стани:
* свіжа позначка (звичайний запис, 1–2 с) — тиша;
* позначка довше 5 хв — банер, один запис у журнал, одне повідомлення власнику;
* той самий епізод на наступних тіках — без повторів;
* усе дописалось — банер зникає, один запис «дописано»;
* пауза синку — тиша (записи не йдуть свідомо);
* позначка старого ID (не дорівнює поточному) — не рахується, як і в рядку.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Order, SyncLog
from app.services import sheet_stuck_writes as stuck
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура
from tests.test_sync import make_session

LATER = stuck.STUCK_AFTER_SECONDS + 1


@pytest.fixture(autouse=True)
def _clean_state():
    stuck.reset()
    yield
    stuck.reset()


@pytest.fixture
def telegram(monkeypatch):
    """Бот увімкнено; що поставлено в чергу власнику."""
    from app.services import telegram_bot

    queued: list[dict] = []
    monkeypatch.setattr(telegram_bot, "bot_enabled", lambda db: True)
    monkeypatch.setattr(telegram_bot, "enqueue", lambda db, **kw: queued.append(kw) or True)
    return queued


def _order(session, **fields) -> Order:
    order = Order(source="lab", sheet_tab="30.09.26", row_number=1, work_order_no="33378", **fields)
    session.add(order)
    session.commit()
    return order


def _logs(session) -> list[SyncLog]:
    return list(session.scalars(select(SyncLog).order_by(SyncLog.id)))


def test_fresh_pending_mark_is_silent(telegram):
    with make_session() as session:
        _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        assert stuck.observe(session, now=0.0) is None
        assert stuck.observe(session, now=LATER - 2) is None
        assert stuck.stuck_sheet_writes() is None
        assert _logs(session) == []
        assert telegram == []


def test_stuck_mark_opens_one_episode_and_closes_on_confirmation(telegram):
    with make_session() as session:
        order = _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        _order(session, sum3d_id="18-00-00", fill_pending="blue")

        stuck.observe(session, now=0.0)
        snap = stuck.observe(session, now=LATER)
        assert snap is not None and (snap.sum3d, snap.fill) == (1, 1)
        assert stuck.stuck_sheet_writes() == snap

        logs = _logs(session)
        assert [log.status for log in logs] == ["warning"]
        assert "Sum3D — 1" in logs[0].message and "синя заливка — 1" in logs[0].message
        assert "33378 (30.09.26)" in logs[0].message
        assert len(telegram) == 1
        assert "chat_id" not in telegram[0]  # без адресата = власнику (telegram_bot.enqueue)

        # Той самий епізод — без повторів у журнал і Telegram.
        stuck.observe(session, now=LATER + 60)
        stuck.observe(session, now=LATER + 120)
        assert len(_logs(session)) == 1
        assert len(telegram) == 1

        # Дописалось: позначки зняті — банер зник, один запис «дописано».
        order.sum3d_pending = None
        for other in session.scalars(select(Order).where(Order.fill_pending.is_not(None))):
            other.fill_pending = None
        session.commit()
        assert stuck.observe(session, now=LATER + 180) is None
        assert stuck.stuck_sheet_writes() is None
        assert [log.status for log in _logs(session)] == ["warning", "ok"]
        assert len(telegram) == 1


def test_a_new_episode_after_recovery_reports_again(telegram):
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
        assert len(telegram) == 2


def test_stale_pending_of_an_old_id_is_not_counted(telegram):
    """Позначка не дорівнює поточному ID — повтор її не пише, рядок не показує;
    тут вона теж не рахується (той самий предикат)."""
    with make_session() as session:
        _order(session, sum3d_id="18-00-00", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        assert stuck.observe(session, now=LATER) is None
        assert telegram == []


def test_archived_work_is_not_counted(telegram):
    from datetime import datetime

    with make_session() as session:
        _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21", archived_at=datetime(2026, 9, 30))
        stuck.observe(session, now=0.0)
        assert stuck.observe(session, now=LATER) is None


def test_paused_sync_is_silent_and_restarts_the_clock(telegram, monkeypatch):
    from app import sync_control

    with make_session() as session:
        _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        monkeypatch.setattr(sync_control, "is_paused", lambda: True)
        assert stuck.observe(session, now=LATER) is None
        monkeypatch.setattr(sync_control, "is_paused", lambda: False)
        # Після паузи відлік заново: повтори теж почались заново.
        assert stuck.observe(session, now=LATER + 1) is None
        assert _logs(session) == []
        assert telegram == []


def test_sync_worker_pause_branch_clears_the_banner(telegram, monkeypatch):
    """Справжній шлях паузи: воркер синку на паузі робить `continue` ДО тіку
    сторожа, тож скидання мусить стояти в самій гілці паузи. Перша версія
    скидала лише всередині `observe` — туди на паузі ніхто не заходить, і
    банер висів би всю паузу."""
    import app.web as web
    from app import sync_control

    with make_session() as session:
        _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        stuck.observe(session, now=LATER)
    assert stuck.stuck_sheet_writes() is not None

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

    assert stuck.stuck_sheet_writes() is None


def test_restart_does_not_resend_the_same_stuck_set_the_same_day(monkeypatch):
    """Рецензія 01.10.26: стан сторожа в памʼяті, а цеховий ПК стартує щоранку.
    Рядок, що не звіряється ніколи, приносив би власнику те саме повідомлення
    після кожного старту. Справжня черга Telegram (не заглушка): той самий
    набір робіт того самого дня — один рядок; новий день — новий."""
    from datetime import datetime

    from app.models import TelegramOutbox
    from app.services import telegram_bot

    monkeypatch.setattr(telegram_bot, "bot_enabled", lambda db: True)
    day1 = datetime(2026, 10, 1, 9, 0)
    with make_session() as session:
        _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        for restart in range(3):                     # три «старти» в різні години
            stuck.reset()
            wall = day1.replace(hour=9 + restart)
            stuck.observe(session, now=0.0, wall=wall)
            stuck.observe(session, now=LATER, wall=wall)
        assert session.query(TelegramOutbox).count() == 1

        stuck.reset()                                # наступний день — новина
        stuck.observe(session, now=0.0, wall=datetime(2026, 10, 2, 9, 0))
        stuck.observe(session, now=LATER, wall=datetime(2026, 10, 2, 9, 0))
        assert session.query(TelegramOutbox).count() == 2


def test_disabled_bot_still_logs_but_queues_nothing(monkeypatch):
    from app.services import telegram_bot

    queued: list = []
    monkeypatch.setattr(telegram_bot, "bot_enabled", lambda db: False)
    monkeypatch.setattr(telegram_bot, "enqueue", lambda db, **kw: queued.append(kw))
    with make_session() as session:
        _order(session, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.observe(session, now=0.0)
        stuck.observe(session, now=LATER)
        assert [log.status for log in _logs(session)] == ["warning"]
        assert queued == []


def test_banner_shows_on_the_queue_and_in_its_poll(app_db, telegram):  # noqa: F811
    """Справжній рендер: банер у сторінці черги й у фрагменті, який полл
    `#mass-vanish-slot` тягне кожні 15 с."""
    app, session_factory = app_db
    with session_factory() as db:
        _order(db, sum3d_id="17-05-21", sum3d_pending="17-05-21")
        stuck.observe(db, now=0.0)
        stuck.observe(db, now=LATER)

    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, html = client.get("/")
    assert status == 200, html[:500]
    assert "stuck-writes-banner" in html
    assert "Sum3D — <b>1</b>" in html

    status, _, fragment = client.get("/sheets/mass-vanish")
    assert status == 200
    assert "stuck-writes-banner" in fragment


def test_no_banner_when_nothing_is_stuck(app_db):  # noqa: F811
    app, _ = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, html = client.get("/")
    assert "stuck-writes-banner" not in html
    _, _, fragment = client.get("/sheets/mass-vanish")
    assert "stuck-writes-banner" not in fragment
