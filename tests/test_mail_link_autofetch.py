"""Фонове скачування файлів за посиланням для відправників з автоскачуванням
(власник 09.11.26, `app/services/mail_link_fetch.py`).

Мережі тут немає: `download_link` підставний. Перевіряємо, КОГО фон бере
(лише довірені, лише нові «Вхідні» після першого запуску, вимикач) і що
невдачі не крутяться вічно.
"""

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

import app.settings_store as settings_store_mod
from app.link_attachments import LinkDownloadError, TransientLinkError
from app.models import Attachment, ClientSenderMemory, EmailMessage
from app.services import mail_link_fetch as mlf

NOW = datetime(2026, 11, 9, 12, 0)
FID = "1LIyJrFNKnY7oFyMadR1W5mRgRpAW9ivl"
BODY = f"Цирконій А2, 3 од. <https://drive.google.com/file/d/{FID}/view>"
TRUSTED = "lab@example.com"


@pytest.fixture
def env(db_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store_mod, "MAIL_ATTACHMENTS_PATH", str(tmp_path / "mail"))
    mlf._reset_attempts_for_tests()
    calls: list[str] = []
    outcome: dict = {"raise": None}

    def fake_download(link, dest, existing_names=frozenset()):
        calls.append(link.file_id or link.url)
        if outcome["raise"] is not None:
            raise outcome["raise"]
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "crown.stl"
        path.write_bytes(b"STL")
        return path

    monkeypatch.setattr(mlf, "download_link", fake_download)
    db = Session(db_engine, expire_on_commit=False)
    db.add(
        ClientSenderMemory(
            sender_key=TRUSTED, client_name="Лаба", auto_accept=True, last_seen_at=NOW
        )
    )
    # Функцію «ввімкнули» годину тому: старіші листи фон не чіпає.
    settings_store_mod.set_setting(db, mlf.SINCE_KEY, (NOW - timedelta(hours=1)).isoformat())
    db.commit()
    yield db, calls, outcome
    db.close()
    mlf._reset_attempts_for_tests()


def _letter(db, *, sender=TRUSTED, received=NOW - timedelta(minutes=5), **over):
    fields = dict(
        uid=str(db.query(EmailMessage).count() + 1),
        uid_validity="1700",
        status="нове",
        attachments_status="ready",
        from_address=sender,
        body_text=BODY,
        received_at=received,
    )
    fields.update(over)
    email = EmailMessage(**fields)
    db.add(email)
    db.commit()
    return email


def test_trusted_sender_link_is_downloaded_in_background(env):
    db, calls, _ = env
    email = _letter(db)

    assert mlf.fetch_pending_once(db, now=NOW) == 1

    assert calls == [FID]
    atts = db.query(Attachment).filter_by(email_message_id=email.id).all()
    assert [a.filename for a in atts] == ["crown.stl"]
    db.refresh(email)
    assert FID in json.loads(email.handled_link_refs)
    # Наступний прохід посилання вже не бере.
    assert mlf.fetch_pending_once(db, now=NOW) == 0
    assert calls == [FID]


@pytest.mark.parametrize(
    "over",
    [
        {"sender": "stranger@example.com"},  # не на автоскачуванні
        {"received": NOW - timedelta(days=1)},  # прийшов до ввімкнення
        {"status": "прийнято"},  # уже в роботі
        {"filter_category": "3D-друк"},  # відфільтрований
        {"attachments_status": "pending"},  # вкладення ще не скачані синком
        {"attachments_status": "skipped"},
        {"inbox_gone_at": NOW},  # покинув «Вхідні»
    ],
)
def test_other_letters_are_left_alone(env, over):
    db, calls, _ = env
    _letter(db, **over)
    assert mlf.fetch_pending_once(db, now=NOW) == 0
    assert calls == []


def test_admin_switch_turns_it_off(env):
    db, calls, _ = env
    _letter(db)
    mlf.set_autofetch_enabled(db, False)
    db.commit()
    assert mlf.fetch_pending_once(db, now=NOW) == 0
    assert calls == []
    mlf.set_autofetch_enabled(db, True)
    db.commit()
    assert mlf.fetch_pending_once(db, now=NOW) == 1


def test_switch_defaults_to_on_and_first_run_stamps_since(db_engine):
    with Session(db_engine) as db:
        assert mlf.autofetch_enabled(db) is True
        assert mlf.autofetch_since(db, NOW) == NOW
        # Штамп записано: наступний виклик не зсуває межу вперед.
        assert mlf.autofetch_since(db, NOW + timedelta(days=3)) == NOW


def test_service_refusal_is_not_retried(env):
    db, calls, outcome = env
    _letter(db)
    outcome["raise"] = LinkDownloadError("файл не розшарено")
    assert mlf.fetch_pending_once(db, now=NOW) == 0
    assert mlf.fetch_pending_once(db, now=NOW + timedelta(hours=5)) == 0
    assert calls == [FID]  # одна спроба, і все: оператор бачить причину в листі


def test_transient_failure_retries_with_pause_and_gives_up(env):
    db, calls, outcome = env
    email = _letter(db)
    outcome["raise"] = TransientLinkError("503")
    t = NOW
    mlf.fetch_pending_once(db, now=t)
    mlf.fetch_pending_once(db, now=t + timedelta(minutes=1))  # ще пауза
    assert len(calls) == 1
    for _ in range(5):
        t += mlf.RETRY_AFTER
        mlf.fetch_pending_once(db, now=t)
    assert len(calls) == mlf.TRANSIENT_ATTEMPTS
    db.refresh(email)
    assert not email.handled_link_refs  # лишився «не скачано» для кнопки


def test_zombie_fetch_gate_blocks_the_pass(env):
    db, calls, _ = env
    _letter(db)
    assert mlf.fetch_pending_once(db, now=NOW, blocked=lambda: "синк ще качає") == 0
    assert calls == []
    assert mlf.fetch_pending_once(db, now=NOW) == 1


# ── Вимикач у «Налаштування → Пошта → Скачування вкладень» (справжній рендер) ──

from tests.test_settings_slabs_render import ADMIN, _open_settings, app_db  # noqa: E402,F401,F811


def test_settings_show_the_switch_and_post_turns_it_off_and_on(app_db):  # noqa: F811
    from tests.asgi_client import MiniClient

    app, session_factory = app_db
    html = _open_settings(app)
    assert 'action="/settings/mail-download/links"' in html
    assert 'name="mail_link_autofetch" value="1" checked' in html  # дефолт увімкнено

    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, _ = client.post("/settings/mail-download/links", {})  # галочку знято
    assert status == 303, status
    with session_factory() as db:
        assert mlf.autofetch_enabled(db) is False
    assert 'name="mail_link_autofetch" value="1" >' in _open_settings(app)

    client.post("/settings/mail-download/links", {"mail_link_autofetch": "1"})
    with session_factory() as db:
        assert mlf.autofetch_enabled(db) is True
