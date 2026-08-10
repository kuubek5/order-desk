from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import User
from app.settings_store import set_setting


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _admin(db: Session) -> User:
    user = User(
        username="admin", password_hash="unused", full_name="Admin", role="адмін"
    )
    db.add(user)
    db.commit()
    return user


def _operator(db: Session) -> User:
    user = User(
        username="operator", password_hash="unused", full_name="Operator", role="оператор"
    )
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None, host: str = "127.0.0.1"):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, client=SimpleNamespace(host=host))


# --- _check_path_status (pure logic, A3) ---------------------------------


def test_check_path_status_neutral_for_empty_path():
    assert web._check_path_status("") == {"state": "neutral", "message": ""}
    assert web._check_path_status("   ") == {"state": "neutral", "message": ""}


def test_check_path_status_errors_when_path_missing(tmp_path):
    missing = tmp_path / "does-not-exist"

    result = web._check_path_status(str(missing))

    assert result["state"] == "error"
    assert "не знайдено" in result["message"]


def test_check_path_status_errors_when_path_is_a_file(tmp_path):
    file_path = tmp_path / "not-a-folder.txt"
    file_path.write_text("x")

    result = web._check_path_status(str(file_path))

    assert result["state"] == "error"
    assert "папка" in result["message"]


def test_check_path_status_succeeds_for_writable_directory(tmp_path):
    result = web._check_path_status(str(tmp_path))

    assert result["state"] == "success"
    # The write-probe marker must never survive the check.
    assert list(tmp_path.iterdir()) == []


def test_check_path_status_warns_when_not_writable(tmp_path, monkeypatch):
    directory = tmp_path / "readonly"
    directory.mkdir()

    def _raise(self, data):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "write_bytes", _raise)

    result = web._check_path_status(str(directory))

    assert result["state"] == "warning"
    assert "запис" in result["message"]


# --- POST /settings/check-path route (A3) ---------------------------------
#
# check_settings_path is a plain `def`, not `async def` — like sync_mail and
# sync_sheets elsewhere in this module, it does blocking I/O (here: a real
# filesystem probe that can stall on an unreachable network share), so it
# must stay off the event loop and let Starlette dispatch it to a
# threadpool. Called directly (no asyncio.run) for the same reason.


def test_check_settings_path_requires_authentication():
    engine = _database()
    with Session(engine) as db, pytest.raises(HTTPException) as exc:
        web.check_settings_path(request=_request(None), kind="export", db=db)
    assert exc.value.status_code == 401


def test_check_settings_path_requires_admin_role():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            web.check_settings_path(
                request=_request(operator.id), kind="export", db=db
            )
    assert exc.value.status_code == 403


def test_check_settings_path_requires_loopback():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            web.check_settings_path(
                request=_request(admin.id, host="203.0.113.5"),
                kind="export",
                db=db,
            )
    assert exc.value.status_code == 403


def test_check_settings_path_reports_success_for_export_folder(tmp_path, monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        context = web.check_settings_path(
            request=_request(admin.id),
            kind="export",
            export_folder_path=str(tmp_path),
            technician_files_path="Z:\\unrelated",
            db=db,
        )
    assert context["result"]["state"] == "success"


def test_check_settings_path_uses_technician_field_for_that_kind(tmp_path, monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    missing = tmp_path / "gone"
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        context = web.check_settings_path(
            request=_request(admin.id),
            kind="technician",
            export_folder_path=str(tmp_path),
            technician_files_path=str(missing),
            db=db,
        )
    assert context["result"]["state"] == "error"


# --- POST /settings/test-imap route (A4) ----------------------------------
#
# Same reasoning as above: test_imap_connection makes a real, potentially
# slow IMAP network login, so it is a plain `def` too and called directly.


def test_test_imap_connection_requires_authentication():
    engine = _database()
    with Session(engine) as db, pytest.raises(HTTPException) as exc:
        web.test_imap_connection(request=_request(None), db=db)
    assert exc.value.status_code == 401


def test_test_imap_connection_requires_admin_role():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            web.test_imap_connection(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


def test_test_imap_connection_reports_error_when_not_configured(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        context = web.test_imap_connection(request=_request(admin.id), db=db)
    assert context["result"]["state"] == "error"
    assert "збережіть" in context["result"]["message"]


def test_test_imap_connection_reports_success_on_login(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "imap_login", "user@ukr.net")
        set_setting(db, "imap_password", "app-password")
        db.commit()

        with patch("app.web.MailBox") as mock_mailbox_cls:
            mock_mailbox_cls.return_value.login.return_value = MagicMock()
            context = web.test_imap_connection(request=_request(admin.id), db=db)

    assert context["result"]["state"] == "success"


def test_test_imap_connection_reports_safe_error_on_failed_login(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "imap_login", "user@ukr.net")
        set_setting(db, "imap_password", "wrong-password")
        db.commit()

        with patch("app.web.MailBox") as mock_mailbox_cls:
            mock_mailbox_cls.return_value.login.side_effect = Exception(
                "AUTHENTICATIONFAILED some raw server detail"
            )
            context = web.test_imap_connection(request=_request(admin.id), db=db)

    assert context["result"]["state"] == "error"
    # The raw IMAP exception text must never leak into the UI-facing message.
    assert "AUTHENTICATIONFAILED" not in context["result"]["message"]
