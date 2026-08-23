from datetime import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Attachment, ClientSenderMemory, EmailMessage, Order


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _trusted(db, key="c@x.ua", folder="Клієнт"):
    db.add(ClientSenderMemory(
        sender_key=key, client_name="Клієнт", export_folder=folder,
        orders_count=3, auto_accept=True, last_seen_at=datetime.now(),
    ))
    db.commit()


def _letter(db, **kw):
    defaults = dict(uid="e", status="нове", from_address="c@x.ua",
                    subject="моно а3", body_text="файл у вкладенні",
                    material_color_guess="моно а3", attachments_status="ready")
    defaults.update(kw)
    e = EmailMessage(**defaults)
    db.add(e); db.flush()
    return e


def test_skip_reasons_cover_every_guardrail(tmp_path):
    with _db() as db:
        _trusted(db)
        # not trusted
        e = _letter(db, from_address="stranger@x.ua")
        assert "не в авто" in web._auto_accept_skip_reason(db, e)
        # trusted but material unknown
        e2 = _letter(db, uid="e2", material_color_guess=None)
        assert "матеріал" in web._auto_accept_skip_reason(db, e2)
        # files behind link
        e3 = _letter(db, uid="e3", body_text="https://drive.google.com/file/d/1LIyJrFNKnY7oFyMadR1W5mRgRpAW9ivl/view")
        assert "посилан" in web._auto_accept_skip_reason(db, e3)
        # still downloading
        e4 = _letter(db, uid="e4", attachments_status="pending")
        assert "вантаж" in web._auto_accept_skip_reason(db, e4)
        # trusted + material + no links + but no files on disk
        e5 = _letter(db, uid="e5")
        assert "немає файлів" in web._auto_accept_skip_reason(db, e5)


def test_auto_accept_pass_accepts_trusted_and_skips_others(tmp_path, monkeypatch):
    export_root = tmp_path / "export"; export_root.mkdir()
    spool = tmp_path / "spool" / "e"; spool.mkdir(parents=True)
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: str(export_root))
    monkeypatch.setattr(web, "open_spreadsheet", lambda db=None: (_ for _ in ()).throw(RuntimeError("no sheet")))
    with _db() as db:
        _trusted(db)
        e = _letter(db)
        f = spool / "crown.stl"; f.write_bytes(b"STL")
        db.add(Attachment(email_message_id=e.id, filename="crown.stl", saved_path=str(f)))
        # a non-trusted letter that must be left alone
        other = _letter(db, uid="o", from_address="stranger@x.ua")
        fo = spool / "x.stl"; fo.write_bytes(b"S")
        db.add(Attachment(email_message_id=other.id, filename="x.stl", saved_path=str(fo)))
        db.commit()

        n = web._auto_accept_pass(db)
        assert n == 1
        db.refresh(e); db.refresh(other)
        assert e.status == "прийнято"
        assert other.status == "нове"  # untouched
        order = db.scalar(select(Order).where(Order.source_email_id == e.id))
        assert order is not None and order.auto_accepted is True
        assert (export_root / "Клієнт").is_dir()  # folder from sender memory


def test_toggle_sender_auto_flips_flag(monkeypatch):
    from types import SimpleNamespace
    with _db() as db:
        db.add(ClientSenderMemory(sender_key="c@x.ua", client_name="C",
                                  orders_count=1, auto_accept=False, last_seen_at=datetime.now()))
        db.commit()
        m = db.scalar(select(ClientSenderMemory))
        from app.models import User
        user = User(username="op", password_hash="x")
        db.add(user); db.commit()
        req = SimpleNamespace(session={"user_id": user.id}, client=SimpleNamespace(host="127.0.0.1"))
        web.toggle_sender_auto(request=req, memory_id=m.id, db=db)
        db.refresh(m); assert m.auto_accept is True
        web.toggle_sender_auto(request=req, memory_id=m.id, db=db)
        db.refresh(m); assert m.auto_accept is False
