"""Журнал переносів листа між папками скриньки (власник 29.09.26).

Два неприйняті листи SmileDent Laba за годину опинились у «Відфрезеровано»,
і відповісти «хто і як» не вдалось нічим: ні перенос через CRM, ні перенос
прямо в пошті не лишали сліду. Тут стережемо, що слід тепер є:

- перенос через CRM — з імʼям оператора;
- перенос, який синк побачив у скриньці, — з позначкою «прямо в пошті»;
- лист, що покинув Вхідні НЕ прийнятим, — статус `warning` (фільтр «увага»).

IMAP замокано — стережемо запис у журнал, не мережу.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app import mail_reader
from app.models import EmailMessage, Order, SyncLog
from app.routers import mail as mail_router_mod
from app.services.mail_folder_journal import VIA_ACCEPT, VIA_CRM, VIA_MAILBOX, log_folder_move
from tests.asgi_client import MiniClient
from tests.test_mail_bulk import _letter
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _journal(db):
    return db.query(SyncLog).filter(SyncLog.direction == "mail_folder").order_by(SyncLog.id).all()


def _pilot_alive(db, *, age=None):
    """Пілот пошти в CRM живий: нещодавно з листа створено роботу
    (`mail_folder_journal.mail_pilot_active`)."""
    from app.business_day import utc_now

    other = _letter(db, f"pilot-{age}", status="прийнято")
    order = Order(source="email", source_email_id=other, client_name="Пілот")
    if age is not None:
        order.created_at = utc_now() - age
    db.add(order)
    db.commit()


def test_crm_bulk_move_names_the_operator_and_flags_unaccepted(app_db, monkeypatch):  # noqa: F811
    """Живий кейс: «↦ Перемістити → Відфрезеровано» по неприйнятих листах.
    Перенесений — рядок «увага» з імʼям оператора; той, що IMAP не переніс, —
    без рядка (переносу не було)."""
    app, session_factory = app_db
    with session_factory() as db:
        moved = _letter(db, "1", subject="Fwd: Кашик мульти колір а3")
        db.get(EmailMessage, moved).from_name = "SmileDent Laba"  # _letter шиє своє імʼя
        db.commit()
        failed = _letter(db, "2")

    def fake_move(db, emails, folder):
        return {e.id: "no such message" for e in emails if e.uid == "2"}

    monkeypatch.setattr(mail_router_mod, "move_messages_to_folder", fake_move)
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, _ = client.post(
        "/mail/bulk", {"action": "move_to", "ids": f"{moved},{failed}", "folder": "Відфрезеровано"}
    )
    assert status == 303

    with session_factory() as db:
        rows = _journal(db)
        assert len(rows) == 1, "лист, який IMAP не переніс, у журнал не йде"
        row = rows[0]
        assert row.status == "warning"
        assert f"лист {moved} " in row.message
        assert "SmileDent Laba" in row.message and "Кашик мульти" in row.message
        assert "«Вхідні» → «Відфрезеровано»" in row.message
        assert f"через CRM, {OPERATOR[0]}" in row.message
        assert "НЕ прийнято" in row.message


def test_sync_marks_a_move_made_directly_in_the_mailbox(app_db, monkeypatch):  # noqa: F811
    """Синк бачить лист у «Відфрезеровано», хоча CRM його туди не клала:
    це перенос прямо в пошті — людини синк не знає, тож так і пише."""
    app, session_factory = app_db
    with session_factory() as db:
        email_id = _letter(db, "7", received_at=datetime.now())
        _pilot_alive(db)

    def fake_ids(mailbox, folder, cutoff):
        return ({"<7@x>"} if folder == "Відфрезеровано" else set()), True

    monkeypatch.setattr(mail_reader, "_folder_message_ids", fake_ids)
    with session_factory() as db:
        changed = mail_reader._reflect_processed_folder(
            db, object(), "Скачано, просчитано", date(2026, 1, 1), milled_folder="Відфрезеровано"
        )
        assert changed == 1
        assert db.get(EmailMessage, email_id).mailbox_folder == "Відфрезеровано"
        rows = _journal(db)
        assert len(rows) == 1
        assert rows[0].status == "warning"
        assert "«Вхідні» → «Відфрезеровано»" in rows[0].message
        assert "прямо в пошті (не через CRM)" in rows[0].message


def test_accepted_letter_move_is_ok_and_same_folder_is_not_logged(app_db):  # noqa: F811
    """Прийнятий лист у папці — норма, не «увага». Перенос «у ту саму папку» —
    не перенос, рядка немає."""
    _, session_factory = app_db
    with session_factory() as db:
        email = db.get(EmailMessage, _letter(db, "9", status="прийнято"))
        log_folder_move(db, email, None, "Скачано, просчитано", via=VIA_ACCEPT, user="Stis")
        log_folder_move(db, email, "Скачано", "Скачано", via=VIA_MAILBOX)
        db.commit()
        rows = _journal(db)
        assert len(rows) == 1
        assert rows[0].status == "ok"
        assert "при прийнятті в чергу, Stis" in rows[0].message
        assert "НЕ прийнято" not in rows[0].message


def test_mailbox_move_is_not_a_warning_while_the_pilot_is_paused(app_db):  # noqa: F811
    """Власник 01.10.26: з 30.09 всю пошту ведуть в ukr.net, роботи вносять у
    таблицю руками — «⚠ НЕ прийнято» висіло на кожному листі (124 за ніч).
    Без прийнять через CRM за 2 доби перенос прямо в пошті — `ok`, рядок
    лишається. Перенос через CRM попереджає, як і раніше."""
    from datetime import timedelta

    _, session_factory = app_db
    with session_factory() as db:
        _pilot_alive(db, age=timedelta(days=3))          # давно — не рахується
        email = db.get(EmailMessage, _letter(db, "21"))
        log_folder_move(db, email, None, "Відфрезеровано", via=VIA_MAILBOX)
        log_folder_move(db, email, None, "Скачано, просчитано", via=VIA_CRM, user="Stis")
        db.commit()
        mailbox, crm = _journal(db)
        assert mailbox.status == "ok"
        assert "прямо в пошті (не через CRM)" in mailbox.message
        assert "НЕ прийнято" not in mailbox.message
        assert crm.status == "warning" and "НЕ прийнято" in crm.message


def test_warning_returns_by_itself_when_a_letter_is_accepted_again(app_db):  # noqa: F811
    _, session_factory = app_db
    with session_factory() as db:
        email = db.get(EmailMessage, _letter(db, "22"))
        log_folder_move(db, email, None, "Відфрезеровано", via=VIA_MAILBOX)
        _pilot_alive(db)                                  # прийняли лист через CRM
        log_folder_move(db, email, "Відфрезеровано", "Скачано, просчитано", via=VIA_MAILBOX)
        db.commit()
        paused, alive = _journal(db)
        assert paused.status == "ok"
        assert alive.status == "warning" and "НЕ прийнято" in alive.message


def test_return_to_inbox_is_journaled_as_ok(app_db):  # noqa: F811
    """Повернення у Вхідні — не ризик (лист знову на видноті), тож `ok`."""
    _, session_factory = app_db
    with session_factory() as db:
        email = db.get(EmailMessage, _letter(db, "11"))
        log_folder_move(db, email, "Відфрезеровано", None, via=VIA_MAILBOX)
        db.commit()
        rows = _journal(db)
        assert rows[0].status == "ok"
        assert "«Відфрезеровано» → «Вхідні»" in rows[0].message
