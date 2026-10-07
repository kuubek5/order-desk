"""Швидше прийняття листа (власник 07.10.26, ACCEPT_SPEEDUP_BRIEF.md).

Два пункти:
  1. перенос прийнятого листа в папку скриньки йде у ФОНІ — прийняття не чекає
     IMAP, а «Повернути» одразу після прийняття не програє гонку фону;
  2. вкладку дня прийняття памʼятає хвилину — не перелічує вкладки на кожен лист.

Фон тут справжній (`conftest` робить його синхронним для всіх інших тестів),
тому база — `check_same_thread=False`.
"""

import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage, Order, SyncLog
from app.routers import mail as mail_router_mod
from app.services import mail_accept as mail_accept_svc
from app.services import mail_folder_journal
from conftest import make_memory_engine
from test_mail_transaction_safety import _accept, _letter, _request, _user, _wire

FOLDER = "Скачано прощитано"


@pytest.fixture
def real_background(monkeypatch):
    """Повернути справжню подачу в пул замість синхронної з conftest."""
    monkeypatch.setattr(
        mail_accept_svc, "_submit_folder_move",
        lambda fn, *args: mail_accept_svc._folder_move_pool.submit(fn, *args),
    )
    yield
    mail_accept_svc.drain_folder_moves()


@pytest.fixture
def shop(tmp_path, monkeypatch):
    engine = make_memory_engine()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mail_accept_svc, "get_setting",
        lambda db, key: FOLDER if key == "mail_processed_folder" else None,
    )
    return SimpleNamespace(engine=engine, mail_root=mail_root, export_root=export_root)


def _hold_pool():
    """Зайняти єдиний потік фону, поки тест не відпустить подію."""
    gate = threading.Event()
    started = threading.Event()

    def _wait():
        started.set()
        gate.wait(10)

    mail_accept_svc._folder_move_pool.submit(_wait)
    assert started.wait(5)
    return gate


def test_accept_does_not_wait_for_imap(shop, monkeypatch, real_background):
    """(а) IMAP спить 2 с — прийняття повертається одразу, лист переїжджає потім."""
    moved = []

    def _slow_move(session, email_message, folder):
        time.sleep(2)
        moved.append((email_message.id, folder))

    monkeypatch.setattr(mail_accept_svc, "move_message_to_folder", _slow_move)

    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, shop.mail_root / "u1")
        started = time.monotonic()
        response = _accept(db, user, email)
        elapsed = time.monotonic() - started
        assert "error=" not in response.headers["location"]
    assert elapsed < 1.0, f"прийняття чекало IMAP: {elapsed:.2f} с"
    assert moved == []  # ще в дорозі

    mail_accept_svc.drain_folder_moves()
    assert moved == [(email.id, FOLDER)]
    with Session(shop.engine) as db:
        stored = db.get(EmailMessage, email.id)
        assert stored.status == "прийнято"
        assert stored.mailbox_folder == FOLDER


def test_restore_before_background_move_keeps_letter_in_inbox(shop, monkeypatch, real_background):
    """(б) Прийняв → одразу «Повернути», поки фон ще не дійшов: фон під локом
    бачить лист «нове» і НЕ переносить його в папку."""
    moved, moved_back = [], []
    monkeypatch.setattr(
        mail_accept_svc, "move_message_to_folder",
        lambda s, e, folder: moved.append(e.id),
    )
    monkeypatch.setattr(
        mail_router_mod, "move_message_back_to_inbox",
        lambda s, e: moved_back.append(e.id),
    )

    gate = _hold_pool()
    try:
        with Session(shop.engine, expire_on_commit=False) as db:
            user = _user(db)
            email, stl = _letter(db, shop.mail_root / "u1")
            assert "error=" not in _accept(db, user, email).headers["location"]
            response = mail_router_mod.restore_email(
                request=_request(user.id), email_id=email.id, db=db,
            )
            assert "error=" not in response.headers["location"]
    finally:
        gate.set()
    mail_accept_svc.drain_folder_moves()

    assert moved == [] and moved_back == []
    with Session(shop.engine) as db:
        stored = db.get(EmailMessage, email.id)
        assert stored.status == "нове"
        assert stored.mailbox_folder is None
        assert db.scalars(select(Order)).all() == []
    assert stl.exists()  # файл повернувся в спул


def test_restore_waits_for_running_move_and_brings_letter_back(shop, monkeypatch, real_background):
    """(б2) Фон уже говорить з IMAP, коли оператор тисне «Повернути»: відкат
    чекає лок і бачить лист УЖЕ в папці — тож повертає його у Вхідні."""
    in_imap = threading.Event()
    release = threading.Event()
    moved_back = []

    def _move(session, email_message, folder):
        in_imap.set()
        release.wait(10)

    monkeypatch.setattr(mail_accept_svc, "move_message_to_folder", _move)

    def _back(session, email_message):
        moved_back.append(email_message.id)
        email_message.mailbox_folder = None

    monkeypatch.setattr(mail_router_mod, "move_message_back_to_inbox", _back)

    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, shop.mail_root / "u1")
        assert "error=" not in _accept(db, user, email).headers["location"]
        user_id, email_id = user.id, email.id
    assert in_imap.wait(5)

    result = {}

    def _restore():
        with Session(shop.engine, expire_on_commit=False) as db2:
            result["response"] = mail_router_mod.restore_email(
                request=_request(user_id), email_id=email_id, db=db2,
            )

    worker = threading.Thread(target=_restore)
    worker.start()
    time.sleep(0.3)
    assert worker.is_alive()  # чекає лок, а не біжить повз фон
    release.set()
    worker.join(10)

    assert "error=" not in result["response"].headers["location"]
    assert moved_back == [email_id]
    with Session(shop.engine) as db:
        stored = db.get(EmailMessage, email_id)
        assert stored.status == "нове"
        assert stored.mailbox_folder is None


def test_background_imap_failure_leaves_sync_log(shop, monkeypatch, real_background):
    """(в) Збій IMAP у фоні — як і раніше: прийняття стоїть, лист у Вхідних,
    слід `mail_to_folder` error у журналі синку."""
    def _boom(session, email_message, folder):
        raise OSError("IMAP недоступний")

    monkeypatch.setattr(mail_accept_svc, "move_message_to_folder", _boom)

    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, shop.mail_root / "u1")
        assert "error=" not in _accept(db, user, email).headers["location"]
    mail_accept_svc.drain_folder_moves()

    with Session(shop.engine) as db:
        stored = db.get(EmailMessage, email.id)
        assert stored.status == "прийнято"
        assert stored.mailbox_folder is None
        assert db.scalar(select(Order)) is not None
        logs = db.scalars(select(SyncLog).where(SyncLog.direction == "mail_to_folder")).all()
        assert [log.status for log in logs] == ["error"]
        assert "IMAP недоступний" in logs[0].message


def test_background_move_journals_and_stamps_time(shop, monkeypatch, real_background):
    """(г) Журнал переносів (via=accept, хто) і `mailbox_moved_at` — як раніше."""
    monkeypatch.setattr(mail_accept_svc, "move_message_to_folder", lambda s, e, f: None)

    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, shop.mail_root / "u1")
        assert "error=" not in _accept(db, user, email).headers["location"]
    mail_accept_svc.drain_folder_moves()

    with Session(shop.engine) as db:
        stored = db.get(EmailMessage, email.id)
        assert stored.mailbox_folder == FOLDER
        assert stored.mailbox_moved_at is not None
        journal = db.scalars(
            select(SyncLog).where(SyncLog.direction == mail_folder_journal.DIRECTION)
        ).all()
        assert len(journal) == 1
        assert journal[0].status == "ok"
        assert f"→ «{FOLDER}»" in journal[0].message
        assert "при прийнятті в чергу, operator" in journal[0].message


def test_partial_accept_does_not_schedule_move(shop, monkeypatch):
    """Лист із нерозібраним кольором лишається у Вхідних — фон навіть не
    ставиться в чергу."""
    scheduled = []
    monkeypatch.setattr(
        mail_accept_svc, "_submit_folder_move", lambda fn, *args: scheduled.append(args),
    )
    from app.models import Attachment

    with Session(shop.engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, shop.mail_root / "u1")
        other = shop.mail_root / "u1" / "bridge.stl"
        other.write_bytes(b"STL2")
        crown = db.scalar(select(Attachment))
        db.add(Attachment(email_message_id=email.id, filename="bridge.stl", saved_path=str(other)))
        db.commit()
        response = mail_router_mod.accept_email(
            confirm_missing="1", request=_request(user.id), email_id=email.id,
            client_name="Люмі-Дент", material_color="моно а3", kind="", quantity="",
            folder_pick="", folder_new="", material_folder="",
            attachment_ids=[crown.id], db=db,
        )
        assert "error=" not in response.headers["location"]
        assert db.get(EmailMessage, email.id).status == "нове"
    assert scheduled == []


# ── Вкладка дня ──────────────────────────────────────────────────────────

def _tab_stubs(monkeypatch):
    calls = SimpleNamespace(listing=0, by_name=[])
    worksheet = SimpleNamespace(title="07.10.26")

    def _listing(spreadsheet, day):
        calls.listing += 1
        return worksheet

    def _by_name(spreadsheet, name):
        calls.by_name.append(name)
        return worksheet

    monkeypatch.setattr(mail_accept_svc, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(mail_accept_svc, "latest_worksheet_on_or_before", _listing)
    monkeypatch.setattr(mail_accept_svc, "get_worksheet_by_name", _by_name)
    return calls, worksheet


def _email():
    return SimpleNamespace(id=1)


def test_two_accepts_in_a_row_list_tabs_once(monkeypatch):
    calls, worksheet = _tab_stubs(monkeypatch)

    first = mail_accept_svc._resolve_target_tab(None, _email())
    second = mail_accept_svc._resolve_target_tab(None, _email())

    assert first == second == ("07.10.26", worksheet)
    assert calls.listing == 1
    assert calls.by_name == ["07.10.26"]


def test_tab_cache_expires_after_a_minute(monkeypatch):
    calls, _ws = _tab_stubs(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(mail_accept_svc.time, "monotonic", lambda: clock[0])

    mail_accept_svc._resolve_target_tab(None, _email())
    clock[0] += mail_accept_svc.TAB_CACHE_SECONDS + 1
    mail_accept_svc._resolve_target_tab(None, _email())

    assert calls.listing == 2


def test_tab_cache_is_dropped_on_new_business_day(monkeypatch):
    from datetime import date

    calls, _ws = _tab_stubs(monkeypatch)
    day = [date(2026, 10, 7)]
    monkeypatch.setattr(mail_accept_svc, "business_today", lambda: day[0])

    mail_accept_svc._resolve_target_tab(None, _email())
    day[0] = date(2026, 10, 8)
    mail_accept_svc._resolve_target_tab(None, _email())

    assert calls.listing == 2


def test_vanished_cached_tab_falls_back_to_listing(monkeypatch):
    calls, worksheet = _tab_stubs(monkeypatch)
    mail_accept_svc._resolve_target_tab(None, _email())
    monkeypatch.setattr(mail_accept_svc, "get_worksheet_by_name", lambda s, n: None)

    assert mail_accept_svc._resolve_target_tab(None, _email()) == ("07.10.26", worksheet)
    assert calls.listing == 2


def test_google_failure_on_cached_tab_keeps_tab_name_without_worksheet(monkeypatch):
    """Як і без кешу: збій Google не блокує прийняття — рядок допише повтор,
    але вкладка вже відома, і робота лягає в неї, а не в «сьогодні»."""
    calls, _ws = _tab_stubs(monkeypatch)
    mail_accept_svc._resolve_target_tab(None, _email())

    def _down(db=None):
        raise RuntimeError("Google недоступний")

    monkeypatch.setattr(mail_accept_svc, "open_spreadsheet", _down)
    assert mail_accept_svc._resolve_target_tab(None, _email()) == ("07.10.26", None)


def test_failed_listing_is_not_cached(monkeypatch):
    calls, _ws = _tab_stubs(monkeypatch)
    monkeypatch.setattr(mail_accept_svc, "latest_worksheet_on_or_before", lambda s, d: None)
    mail_accept_svc._resolve_target_tab(None, _email())
    assert mail_accept_svc._tab_cache is None
