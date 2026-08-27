from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ClientSenderMemory, EmailMessage
from app.sender_memory import lookup_sender, remember_sender, sender_key_for


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def test_key_is_lowercased_address_for_direct_mail():
    e = EmailMessage(uid="1", from_address="Lumi@UKR.net", subject="моно а3")
    assert sender_key_for(e) == "lumi@ukr.net"


def test_key_folds_original_sender_for_forwarded_mail():
    e = EmailMessage(
        uid="1", from_address="admin@lab.ua", subject="Fwd: pmma a2",
        body_text="From: Стоматологія Люмі <lumi@ukr.net>\nфайли",
    )
    assert sender_key_for(e) == "admin@lab.ua|стоматологія люмі"


def test_forward_without_original_sender_yields_no_key():
    """A forwarder relays many clients — without the quoted From: the address
    alone would mislead, so the memory stays silent."""
    e = EmailMessage(uid="1", from_address="admin@lab.ua", subject="Fwd:", body_text="just files")
    assert sender_key_for(e) is None
    assert sender_key_for(EmailMessage(uid="2", from_address=None)) is None


def test_remember_then_lookup_round_trip_and_upsert():
    with _db() as db:
        first = EmailMessage(uid="a", from_address="lumi@ukr.net", subject="моно а3")
        db.add(first)
        db.commit()
        assert lookup_sender(db, first) is None  # unknown yet

        remember_sender(db, first, "Люмі-Дент", "Люмі-Дент", now=datetime(2026, 8, 1))
        db.commit()
        hint = lookup_sender(db, first)
        assert (hint.client_name, hint.export_folder, hint.orders_count) == ("Люмі-Дент", "Люмі-Дент", 1)

        # second accept from the same sender: latest correction wins, count grows
        second = EmailMessage(uid="b", from_address="LUMI@ukr.net", subject="emo a2")
        db.add(second)
        db.commit()
        remember_sender(db, second, "Люмі Дент (Київ)", "Люмі-Дент", now=datetime(2026, 8, 14))
        db.commit()
        hint = lookup_sender(db, second)
        assert hint.client_name == "Люмі Дент (Київ)"
        assert hint.orders_count == 2
        assert hint.last_seen_at == datetime(2026, 8, 14)
        assert db.scalar(select(ClientSenderMemory.sender_key)) == "lumi@ukr.net"


def test_remember_keeps_folder_when_new_accept_has_none():
    with _db() as db:
        e = EmailMessage(uid="a", from_address="x@y.z", subject="s")
        db.add(e)
        db.commit()
        remember_sender(db, e, "X", "X-folder")
        db.commit()
        remember_sender(db, e, "X", None)
        db.commit()
        assert lookup_sender(db, e).export_folder == "X-folder"


def test_remember_ignores_blank_name():
    with _db() as db:
        e = EmailMessage(uid="a", from_address="x@y.z", subject="s")
        db.add(e)
        db.commit()
        assert remember_sender(db, e, "   ", "f") is None
