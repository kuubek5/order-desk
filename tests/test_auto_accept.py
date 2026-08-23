from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Attachment, ClientSenderMemory, EmailMessage, Order, User


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
    d = dict(uid="e", status="нове", from_address="c@x.ua", subject="моно а3",
             body_text="файл у вкладенні", material_color_guess="моно а3",
             attachments_status="ready")
    d.update(kw)
    e = EmailMessage(**d); db.add(e); db.flush()
    return e


def _req(user_id):
    from types import SimpleNamespace
    return SimpleNamespace(session={"user_id": user_id}, client=SimpleNamespace(host="127.0.0.1"))


def test_download_skip_reasons_cover_guardrails():
    with _db() as db:
        _trusted(db)
        assert "не в авто" in web._auto_download_skip_reason(db, _letter(db, from_address="s@x.ua"))
        assert "матеріал" in web._auto_download_skip_reason(db, _letter(db, uid="e2", material_color_guess=None))
        assert "посилан" in web._auto_download_skip_reason(
            db, _letter(db, uid="e3", body_text="https://drive.google.com/file/d/1LIyJrFNKnY7oFyMadR1W5mRgRpAW9ivl/view"))
        assert "вантаж" in web._auto_download_skip_reason(db, _letter(db, uid="e4", attachments_status="pending"))
        assert "спул" in web._auto_download_skip_reason(db, _letter(db, uid="e5"))


def test_auto_download_stages_files_but_never_accepts(tmp_path, monkeypatch):
    export_root = tmp_path / "export"; export_root.mkdir()
    spool = tmp_path / "spool" / "e"; spool.mkdir(parents=True)
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: str(export_root))
    with _db() as db:
        _trusted(db)
        e = _letter(db)
        f = spool / "crown.stl"; f.write_bytes(b"STL")
        att = Attachment(email_message_id=e.id, filename="crown.stl", saved_path=str(f))
        db.add(att)
        other = _letter(db, uid="o", from_address="s@x.ua")
        fo = spool / "x.stl"; fo.write_bytes(b"S")
        db.add(Attachment(email_message_id=other.id, filename="x.stl", saved_path=str(fo)))
        db.commit()

        n = web._auto_download_pass(db)
        assert n == 1
        db.refresh(e); db.refresh(att); db.refresh(other)
        # trusted letter: files staged to export, but NO order, still "нове"
        assert e.status == "нове"
        assert att.staged_to_export is True
        assert Path(att.saved_path).is_file() and export_root in Path(att.saved_path).parents
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert (export_root / "Клієнт").is_dir()
        # non-trusted untouched
        assert other.status == "нове"

        # a second pass does nothing (already staged)
        assert web._auto_download_pass(db) == 0


def test_manual_accept_links_staged_files_without_moving(tmp_path, monkeypatch):
    export_root = tmp_path / "export"; export_root.mkdir()
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: str(export_root))
    monkeypatch.setattr(web, "open_spreadsheet", lambda db=None: (_ for _ in ()).throw(RuntimeError("no sheet")))
    with _db() as db:
        user = User(username="op", password_hash="x"); db.add(user)
        e = _letter(db, status="нове")
        # a pre-staged file already sitting in export
        staged_dir = export_root / "Клієнт" / "01.01.26" / "моно а3"; staged_dir.mkdir(parents=True)
        sf = staged_dir / "crown.stl"; sf.write_bytes(b"STL")
        att = Attachment(email_message_id=e.id, filename="crown.stl", saved_path=str(sf), staged_to_export=True)
        db.add(att); db.commit()
        before_path = att.saved_path

        import asyncio
        asyncio.run(web.accept_email(
            request=_req(user.id), email_id=e.id, client_name="Клієнт",
            material_color="моно а3", kind="", quantity="", folder_pick="",
            folder_new="", material_folder="", attachment_ids=[], db=db,
        ))
        db.refresh(e); db.refresh(att)
        assert e.status == "прийнято"
        order = db.scalar(select(Order).where(Order.source_email_id == e.id))
        assert order is not None
        assert att.order_id == order.id
        assert att.saved_path == before_path  # NOT moved again


def test_add_sender_by_email_and_toggle(monkeypatch):
    with _db() as db:
        user = User(username="op", password_hash="x"); db.add(user); db.commit()
        web.add_sender_auto(request=_req(user.id), email_address="New@Client.UA", db=db)
        row = db.scalar(select(ClientSenderMemory))
        assert row.sender_key == "new@client.ua" and row.auto_accept is True
        assert row.orders_count == 0
        # toggle off then on
        web.toggle_sender_auto(request=_req(user.id), memory_id=row.id, db=db)
        db.refresh(row); assert row.auto_accept is False
        # adding again just switches on (idempotent)
        web.add_sender_auto(request=_req(user.id), email_address="new@client.ua", db=db)
        db.refresh(row); assert row.auto_accept is True
        assert db.scalar(select(func.count()).select_from(ClientSenderMemory)) == 1
