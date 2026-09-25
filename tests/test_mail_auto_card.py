"""Автоскачування з картки листа й імена у вкладці «Авто» (власник 25.09.26).

«Маленька кнопочка, яка додає клієнта до автоскачування» — перемикач у шапці
картки; «щоб тут відображалось імʼя клієнта, не тільки емейл» — у списку.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import Client, ClientSenderMemory, EmailMessage
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _letter(db, uid="1", address="ipad.galiy@gmail.com", from_name=""):
    email = EmailMessage(uid=uid, uid_validity="1", from_address=address, from_name=from_name,
                         subject="робота", status="нове", attachments_status="ready",
                         message_id=f"<{uid}@x>")
    db.add(email)
    db.commit()
    return email.id


def test_card_toggle_adds_sender_with_real_name_and_flips(app_db):  # noqa: F811
    app, factory = app_db
    with factory() as db:
        db.add(Client(canonical_name="Oleksandr", email="ipad.galiy@gmail.com"))
        db.commit()
        eid = _letter(db)
    client = MiniClient(app)
    client.login(*ADMIN)

    _, _, card = client.get(f"/mail/{eid}?panel=1")
    assert f'hx-post="/mail/{eid}/auto-download"' in card
    assert 'class="autotoggle mini ' in card and 'autotoggle mini on' not in card

    status, _, frag = client.post(f"/mail/{eid}/auto-download", {})
    assert status == 200 and 'autotoggle mini on' in frag
    with factory() as db:
        row = db.scalar(select(ClientSenderMemory))
        assert row.sender_key == "ipad.galiy@gmail.com"
        assert row.client_name == "Oleksandr"  # не адреса-заглушка
        assert row.auto_accept is True

    _, _, frag = client.post(f"/mail/{eid}/auto-download", {})
    assert 'autotoggle mini on' not in frag
    with factory() as db:
        assert db.scalar(select(ClientSenderMemory)).auto_accept is False


def test_auto_tab_shows_client_names_not_addresses(app_db):  # noqa: F811
    app, factory = app_db
    with factory() as db:
        db.add(Client(canonical_name="Oleksandr", email="ipad.galiy@gmail.com"))
        now = datetime(2026, 9, 25, 12, 0)
        for key in ("ipad.galiy@gmail.com", "vitas00172@gmail.com", "nobody@x.com"):
            db.add(ClientSenderMemory(sender_key=key, client_name=key, export_folder=None,
                                      orders_count=0, auto_accept=True, last_seen_at=now))
        db.commit()
        _letter(db, "2", "vitas00172@gmail.com", "Vitaliy Tarasenko")
    client = MiniClient(app)
    client.login(*ADMIN)
    _, _, page = client.get("/mail?view=auto")
    assert '<span class="sr-name">Oleksandr</span>' in page        # з картки клієнта
    assert '<span class="sr-name">Vitaliy Tarasenko</span>' in page  # з підпису листа
    assert '<span class="sr-name">nobody@x.com</span>' in page       # більше нічого немає
