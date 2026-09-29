"""Лист у Вхідних не може бути «у папці» (SmileDent Laba, 29.09.26).

Два неприйняті листи лежали у Вхідних ukr.net, а CRM показувала їх у
«Відфрезеровано». Дірка: дзеркало папок (`_reflect_processed_folder`) шукає лист
у папках лише за Message-ID і не питає, чи він досі у Вхідних. Коли в папці є
інший лист із тим самим Message-ID (клієнт надіслав той самий лист удруге,
копія листа в пошті, поштовий клієнт відправника шиє однаковий заголовок), лист
із Вхідних мовчки ставав «перенесеним» і зникав із тріажу.

Правило: лист, чий UID є у свіжій вибірці Вхідних (тієї ж нумерації), фізично
у Вхідних — і це перемагає будь-який збіг Message-ID у папці. Вже хибно
позначені листи повертаються в тріаж самі.

IMAP замокано — стережемо рішення дзеркала, не мережу.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app import mail_reader
from app.models import EmailMessage, SyncLog
from tests.test_mail_bulk import _letter
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")

MILLED = "Відфрезеровано"
PROCESSED = "Скачано, просчитано"


def _folders(monkeypatch, milled_mids):
    def fake_ids(mailbox, folder, cutoff):
        return (set(milled_mids) if folder == MILLED else set()), True

    monkeypatch.setattr(mail_reader, "_folder_message_ids", fake_ids)


def _reflect(db, **kw):
    return mail_reader._reflect_processed_folder(
        db, object(), PROCESSED, date(2026, 1, 1), milled_folder=MILLED, **kw
    )


def test_letter_still_in_inbox_is_not_marked_by_a_twin_in_the_folder(app_db, monkeypatch):  # noqa: F811
    """Живий кейс: у «Відфрезеровано» лежить лист із тим самим Message-ID,
    а цей — досі у Вхідних (його UID у вибірці). Лишається в тріажі."""
    _, session_factory = app_db
    with session_factory() as db:
        email_id = _letter(db, "40", received_at=datetime.now())
        db.get(EmailMessage, email_id).message_id = "<same@smile>"  # _letter шиє свій
        db.commit()
    _folders(monkeypatch, {"<same@smile>"})
    with session_factory() as db:
        changed = _reflect(db, inbox_uids={"40"}, inbox_validity="1", inbox_seen_at=datetime.now())
        assert changed == 0
        assert db.get(EmailMessage, email_id).mailbox_folder is None


def test_letter_that_really_left_inbox_is_still_mirrored(app_db, monkeypatch):  # noqa: F811
    """Те, що вже працювало: лист зник зі Вхідних і лежить у папці — переноситься."""
    _, session_factory = app_db
    with session_factory() as db:
        email_id = _letter(db, "41", received_at=datetime.now())
    _folders(monkeypatch, {"<41@x>"})
    with session_factory() as db:
        changed = _reflect(db, inbox_uids={"99"}, inbox_validity="1", inbox_seen_at=datetime.now())
        assert changed == 1
        assert db.get(EmailMessage, email_id).mailbox_folder == MILLED


def test_wrongly_marked_letter_returns_to_triage_by_itself(app_db, monkeypatch):  # noqa: F811
    """Вже зіпсовані на проді листи: у базі «Відфрезеровано», а UID досі у
    Вхідних — повертаються в тріаж без чиїхось рук, з рядком у журналі."""
    _, session_factory = app_db
    with session_factory() as db:
        email_id = _letter(
            db, "42", folder=MILLED, received_at=datetime.now(),
            mailbox_moved_at=datetime.now() - timedelta(hours=2),
        )
        db.get(EmailMessage, email_id).message_id = "<same@smile>"
        db.commit()
    _folders(monkeypatch, {"<same@smile>"})
    with session_factory() as db:
        changed = _reflect(db, inbox_uids={"42"}, inbox_validity="1", inbox_seen_at=datetime.now())
        assert changed == 1
        row = db.get(EmailMessage, email_id)
        assert row.mailbox_folder is None and row.mailbox_moved_at is None
        log = db.query(SyncLog).filter(SyncLog.direction == "mail_folder").one()
        assert f"«{MILLED}» → «Вхідні»" in log.message
        assert "CRM виправила власну хибну мітку" in log.message
        assert "прямо в пошті" not in log.message


def test_move_made_after_the_inbox_read_is_not_undone(app_db, monkeypatch):  # noqa: F811
    """Гонка: оператор переніс лист кнопкою CRM, поки синк ще йшов — вибірка
    Вхідних старша за перенос і вже бреше. Такий лист не повертаємо."""
    _, session_factory = app_db
    seen = datetime.now() - timedelta(minutes=1)
    with session_factory() as db:
        email_id = _letter(
            db, "43", folder=MILLED, received_at=datetime.now(), mailbox_moved_at=datetime.now(),
        )
    _folders(monkeypatch, {"<43@x>"})
    with session_factory() as db:
        assert _reflect(db, inbox_uids={"43"}, inbox_validity="1", inbox_seen_at=seen) == 0
        assert db.get(EmailMessage, email_id).mailbox_folder == MILLED


def test_uid_from_another_numbering_proves_nothing(app_db, monkeypatch):  # noqa: F811
    """UID з мертвої нумерації (UIDVALIDITY змінився) — не доказ, що лист у Вхідних."""
    _, session_factory = app_db
    with session_factory() as db:
        email_id = _letter(db, "44", received_at=datetime.now())
    _folders(monkeypatch, {"<44@x>"})
    with session_factory() as db:
        assert _reflect(db, inbox_uids={"44"}, inbox_validity="2", inbox_seen_at=datetime.now()) == 1
        assert db.get(EmailMessage, email_id).mailbox_folder == MILLED
