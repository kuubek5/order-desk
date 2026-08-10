from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Attachment, EmailMessage, Order, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Operator")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None, host: str = "127.0.0.1"):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, client=SimpleNamespace(host=host))


def test_queue_eagerly_exposes_pending_mail_oldest_first_independent_of_filters(
    tmp_path, monkeypatch
):
    engine = _database()
    mail_root = tmp_path / "mail"
    mail_root.mkdir()
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        now = datetime.now()
        newer = EmailMessage(uid="newer", status="нове", received_at=now)
        older = EmailMessage(uid="older", status="нове", received_at=now - timedelta(hours=1))
        accepted = EmailMessage(uid="accepted", status="прийнято", received_at=now - timedelta(days=1))
        db.add_all([newer, older, accepted])
        db.add(Order(source="lab", sheet_tab=date.today().strftime("%d.%m.%y")))
        db.commit()

        context = web.get_queue(
            request=_request(user.id), period="today", ready="all", source="lab", db=db
        )

        assert [email.uid for email in context["pending_emails"]] == ["older", "newer"]
        assert context["pending_mail_count"] == 2
        assert all(email.folder_available is False for email in context["pending_emails"])


def test_open_mail_folder_requires_authentication(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine) as db, pytest.raises(HTTPException) as exc:
        web.open_mail_folder(request=_request(None), email_id=1, db=db)
    assert exc.value.status_code == 401


def test_open_mail_folder_rejects_non_loopback(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        with pytest.raises(HTTPException) as exc:
            web.open_mail_folder(
                request=_request(user.id, "192.168.1.20"), email_id=1, db=db
            )
    assert exc.value.status_code == 403


def test_open_mail_folder_opens_safe_db_path_and_returns_no_content(tmp_path, monkeypatch):
    engine = _database()
    mail_root = tmp_path / "mail"
    folder = mail_root / "message-1"
    folder.mkdir(parents=True)
    file = folder / "case.stl"
    file.write_bytes(b"mesh")
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    opened: list = []
    monkeypatch.setattr(web, "_open_folder_in_explorer", opened.append)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="mail-1", status="нове")
        db.add(email)
        db.flush()
        db.add(
            Attachment(
                email_message_id=email.id,
                filename="case.stl",
                saved_path=str(file),
            )
        )
        db.commit()

        response = web.open_mail_folder(
            request=_request(user.id), email_id=email.id, db=db
        )

    assert response.status_code == 204
    assert opened == [folder.resolve()]


def test_open_mail_folder_rejects_db_path_outside_roots(tmp_path, monkeypatch):
    engine = _database()
    mail_root = tmp_path / "mail"
    mail_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    file = outside / "case.stl"
    file.write_bytes(b"mesh")
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="mail-1", status="нове")
        db.add(email)
        db.flush()
        db.add(Attachment(email_message_id=email.id, filename="case.stl", saved_path=str(file)))
        db.commit()

        with pytest.raises(HTTPException) as exc:
            web.open_mail_folder(request=_request(user.id), email_id=email.id, db=db)

    assert exc.value.status_code == 404


def test_open_mail_folder_reports_non_windows_backend(tmp_path, monkeypatch):
    engine = _database()
    mail_root = tmp_path / "mail"
    mail_root.mkdir()
    file = mail_root / "case.stl"
    file.write_bytes(b"mesh")
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    monkeypatch.setattr(
        web, "_open_folder_in_explorer", lambda _folder: (_ for _ in ()).throw(NotImplementedError())
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="mail-1", status="нове")
        db.add(email)
        db.flush()
        db.add(Attachment(email_message_id=email.id, filename="case.stl", saved_path=str(file)))
        db.commit()

        with pytest.raises(HTTPException) as exc:
            web.open_mail_folder(request=_request(user.id), email_id=email.id, db=db)

    assert exc.value.status_code == 501
