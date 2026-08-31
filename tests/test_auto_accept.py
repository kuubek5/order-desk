from datetime import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.routers import mail as mail_router_mod
from app.db import Base
from app.models import ClientSenderMemory, EmailMessage, User
from app.sender_memory import is_auto_sender


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _req(user_id):
    from types import SimpleNamespace
    return SimpleNamespace(session={"user_id": user_id}, client=SimpleNamespace(host="127.0.0.1"))


def test_is_auto_sender_matches_base_address_and_composite_key():
    with _db() as db:
        # trust a forwarder's bare address
        db.add(ClientSenderMemory(sender_key="admin@lab.ua", client_name="Admin",
                                  orders_count=0, auto_accept=True, last_seen_at=datetime.now()))
        db.commit()
        # a forwarded letter from that forwarder (composite key differs) still matches
        fwd = EmailMessage(uid="1", from_address="admin@lab.ua", subject="Fwd: pmma a2",
                           body_text="From: Клініка <c@ukr.net>\nфайли")
        assert is_auto_sender(db, fwd) is True
        # direct mail from the same address matches too
        direct = EmailMessage(uid="2", from_address="Admin@Lab.ua", subject="pmma a2")
        assert is_auto_sender(db, direct) is True
        # a stranger does not
        assert is_auto_sender(db, EmailMessage(uid="3", from_address="x@y.z")) is False
        # disabled row does not match
        db.query(ClientSenderMemory).update({"auto_accept": False})
        db.commit()
        assert is_auto_sender(db, direct) is False


def test_add_sender_by_email_and_toggle():
    with _db() as db:
        user = User(username="op", password_hash="x")
        db.add(user)
        db.commit()
        mail_router_mod.add_sender_auto(request=_req(user.id), email_address="New@Client.UA", db=db)
        row = db.scalar(select(ClientSenderMemory))
        assert row.sender_key == "new@client.ua" and row.auto_accept is True
        assert row.orders_count == 0
        mail_router_mod.toggle_sender_auto(request=_req(user.id), memory_id=row.id, db=db)
        db.refresh(row)
        assert row.auto_accept is False
        # adding again just switches on (idempotent, no duplicate)
        mail_router_mod.add_sender_auto(request=_req(user.id), email_address="new@client.ua", db=db)
        db.refresh(row)
        assert row.auto_accept is True
        assert db.scalar(select(func.count()).select_from(ClientSenderMemory)) == 1
