"""Ввімкнули «Авто» — докачуються листи відправника, що вже чекають (08.10.26).

Власник: «декільком клієнтам поставив авто, пройшов час, а воно не скачалось».
Синк вирішує «качати чи ні» лише коли лист щойно прийшов, тож листи, що лежали
у «Вхідних» до галочки, лишались без файлів.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import ClientSenderMemory, EmailMessage
from app.services import mail_auto_backfill
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _letter(db, uid, address, **kw):
    fields = dict(uid=uid, uid_validity="1", from_address=address, subject="робота",
                  status="нове", attachments_status="skipped", message_id=f"<{uid}@x>")
    fields.update(kw)
    email = EmailMessage(**fields)
    db.add(email)
    db.commit()
    return email.id


def test_only_waiting_letters_of_that_sender_are_picked(app_db):  # noqa: F811
    _, factory = app_db
    with factory() as db:
        waiting = _letter(db, "1", "Lumi@UKR.net")
        waiting2 = _letter(db, "2", "lumi@ukr.net")
        _letter(db, "3", "lumi@ukr.net", attachments_status="ready")          # файли вже є
        _letter(db, "4", "lumi@ukr.net", status="прийнято")                    # уже в черзі
        _letter(db, "5", "lumi@ukr.net", status="відхилено")
        _letter(db, "6", "lumi@ukr.net", mailbox_folder="Скачено")             # не у «Вхідних»
        _letter(db, "7", "other@ukr.net")                                      # чужий відправник
        ids = mail_auto_backfill.waiting_letters(db, {"lumi@ukr.net"})
    assert ids == [waiting2, waiting]  # найновіші перші


def test_backfill_downloads_and_skips_already_handled(app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    with factory() as db:
        a = _letter(db, "1", "lumi@ukr.net")
        b = _letter(db, "2", "lumi@ukr.net")
        bind = db.get_bind()
    pulled: list[int] = []

    def fake_download(session, email, root):
        pulled.append(email.id)
        email.attachments_status = "ready"
        return 3

    import app.mail_reader as mail_reader
    monkeypatch.setattr(mail_reader, "download_attachments_now", fake_download)
    # Оператор устиг скачати «b» руками раніше за фон.
    with factory() as db:
        db.get(EmailMessage, b).attachments_status = "ready"
        db.commit()

    mail_auto_backfill._backfill(bind, [a, b])

    assert pulled == [a]
    with factory() as db:
        assert db.get(EmailMessage, a).attachments_status == "ready"


def test_one_failed_letter_does_not_stop_the_rest(app_db, monkeypatch):  # noqa: F811
    _, factory = app_db
    with factory() as db:
        a = _letter(db, "1", "lumi@ukr.net")
        b = _letter(db, "2", "lumi@ukr.net")
        bind = db.get_bind()

    def flaky(session, email, root):
        if email.id == a:
            raise RuntimeError("Лист більше недоступний на сервері")
        email.attachments_status = "ready"
        return 1

    import app.mail_reader as mail_reader
    monkeypatch.setattr(mail_reader, "download_attachments_now", flaky)
    mail_auto_backfill._backfill(bind, [a, b])
    with factory() as db:
        assert db.get(EmailMessage, a).attachments_status == "skipped"
        assert db.get(EmailMessage, b).attachments_status == "ready"


def test_card_toggle_on_queues_waiting_letters_off_does_not(app_db):  # noqa: F811
    app, factory = app_db
    with factory() as db:
        opened = _letter(db, "1", "ipad.galiy@gmail.com")
        older = _letter(db, "2", "ipad.galiy@gmail.com")
    client = MiniClient(app)
    client.login(*ADMIN)

    status, _, _ = client.post(f"/mail/{opened}/auto-download", {})
    assert status == 200
    assert len(mail_auto_backfill.recorded) == 1
    _bind, ids = mail_auto_backfill.recorded[0]
    assert sorted(ids) == sorted([opened, older])

    client.post(f"/mail/{opened}/auto-download", {})  # вимкнули
    assert len(mail_auto_backfill.recorded) == 1


def test_auto_tab_toggle_and_manual_add_queue_waiting_letters(app_db):  # noqa: F811
    app, factory = app_db
    with factory() as db:
        a = _letter(db, "1", "vitas00172@gmail.com")
        b = _letter(db, "2", "new@client.ua")
        from datetime import datetime
        db.add(ClientSenderMemory(sender_key="vitas00172@gmail.com", client_name="Vitaliy",
                                  export_folder=None, orders_count=1, auto_accept=False,
                                  last_seen_at=datetime(2026, 10, 7, 12, 0)))
        db.commit()
        memory_id = db.scalar(select(ClientSenderMemory.id))
    client = MiniClient(app)
    client.login(*ADMIN)

    client.post(f"/mail/senders/{memory_id}/auto", {})
    client.post("/mail/senders/add", {"email_address": "New@Client.ua"})

    queued = [ids for _bind, ids in mail_auto_backfill.recorded]
    assert queued == [[a], [b]]
