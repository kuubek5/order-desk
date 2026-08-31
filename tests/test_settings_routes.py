import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.settings_store import get_imap_login, get_imap_password
from app.routers import settings as settings_router_mod
from app.db import Base
from app.google_oauth import OAuthFlowError
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
    assert settings_router_mod.check_path_status("") == {"state": "neutral", "message": ""}
    assert settings_router_mod.check_path_status("   ") == {"state": "neutral", "message": ""}


def test_check_path_status_errors_when_path_missing(tmp_path):
    missing = tmp_path / "does-not-exist"

    result = settings_router_mod.check_path_status(str(missing))

    assert result["state"] == "error"
    assert "не знайдено" in result["message"]


def test_check_path_status_errors_when_path_is_a_file(tmp_path):
    file_path = tmp_path / "not-a-folder.txt"
    file_path.write_text("x")

    result = settings_router_mod.check_path_status(str(file_path))

    assert result["state"] == "error"
    assert "папка" in result["message"]


def test_check_path_status_succeeds_for_writable_directory(tmp_path):
    result = settings_router_mod.check_path_status(str(tmp_path))

    assert result["state"] == "success"
    # The write-probe marker must never survive the check.
    assert list(tmp_path.iterdir()) == []


def test_check_path_status_warns_when_not_writable(tmp_path, monkeypatch):
    directory = tmp_path / "readonly"
    directory.mkdir()

    def _raise(self, data):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "write_bytes", _raise)

    result = settings_router_mod.check_path_status(str(directory))

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
        settings_router_mod.check_settings_path(request=_request(None), kind="export", db=db)
    assert exc.value.status_code == 401


def test_check_settings_path_allows_operator_role(tmp_path, monkeypatch):
    """Paths are operator-editable (OPERATOR_EDITABLE_KEYS) — unlike every
    other /settings action, this one must NOT 403 for a plain operator."""
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        context = settings_router_mod.check_settings_path(
            request=_request(operator.id),
            kind="export",
            export_folder_path=str(tmp_path),
            technician_files_path="",
            db=db,
        )
    assert context["result"]["state"] == "success"


def test_check_settings_path_requires_loopback():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.check_settings_path(
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
        context = settings_router_mod.check_settings_path(
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
        context = settings_router_mod.check_settings_path(
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
        settings_router_mod.test_imap_connection(request=_request(None), db=db)
    assert exc.value.status_code == 401


def test_test_imap_connection_requires_admin_role():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.test_imap_connection(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


def test_test_imap_connection_reports_error_when_not_configured(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        context = settings_router_mod.test_imap_connection(request=_request(admin.id), db=db)
    assert context["result"]["state"] == "error"
    assert "логін і пароль" in context["result"]["message"]


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

        with patch("app.routers.settings.MailBox") as mock_mailbox_cls:
            mock_mailbox_cls.return_value.login.return_value = MagicMock()
            context = settings_router_mod.test_imap_connection(request=_request(admin.id), db=db)

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

        with patch("app.routers.settings.MailBox") as mock_mailbox_cls:
            mock_mailbox_cls.return_value.login.side_effect = Exception(
                "AUTHENTICATIONFAILED some raw server detail"
            )
            context = settings_router_mod.test_imap_connection(request=_request(admin.id), db=db)

    assert context["result"]["state"] == "error"
    # The raw IMAP exception text must never leak into the UI-facing message.
    assert "AUTHENTICATIONFAILED" not in context["result"]["message"]


# --- _imap_error_reason (classifier) + POST /settings/imap (HTMX save) ------
#
# The lab operator must see WHICH problem it is (rejected app-password vs no
# internet) and never a silent scroll-to-top reload. These cover the classifier
# and the save route that fixes both.


class _FakeResp:
    """Stand-in for a Starlette response: carries the template context and a
    real headers dict so HX-Trigger assertions work without a live request."""

    def __init__(self, context):
        self.context = context
        self.headers = {}


def _fake_template_response(monkeypatch):
    monkeypatch.setattr(
        web.templates,
        "TemplateResponse",
        lambda request, template, context: _FakeResp(context),
    )


def _imap_request(user_id, form_data, host="127.0.0.1"):
    async def _form():
        return form_data

    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(
        session=session, client=SimpleNamespace(host=host), form=_form
    )


def test_imap_error_reason_classifies_login_rejection_without_leaking_raw():
    from imap_tools.errors import MailboxLoginError

    exc = MailboxLoginError(
        command_result=("NO", [b"AUTHENTICATIONFAILED raw server detail"]),
        expected="OK",
    )
    msg = settings_router_mod._imap_error_reason(exc)
    assert "ukr.net" in msg
    assert "пароль для програм" in msg
    assert "AUTHENTICATIONFAILED" not in msg


def test_imap_error_reason_distinguishes_network_from_auth():
    assert "інтернет" in settings_router_mod._imap_error_reason(TimeoutError())
    assert "з'єднання" in settings_router_mod._imap_error_reason(ConnectionError("boom"))


def test_save_imap_settings_requires_admin():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        req = _imap_request(operator.id, {"imap_login": "a@ukr.net", "imap_password": "p"})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(settings_router_mod.save_imap_settings(request=req, db=db))
    assert exc.value.status_code == 403


def test_save_imap_settings_success_fires_toast_and_persists(monkeypatch):
    engine = _database()
    _fake_template_response(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        req = _imap_request(
            admin.id, {"imap_login": "user@ukr.net", "imap_password": "app-pw"}
        )
        with patch("app.routers.settings.MailBox") as mock_mailbox_cls:
            mock_mailbox_cls.return_value.login.return_value = MagicMock()
            resp = asyncio.run(settings_router_mod.save_imap_settings(request=req, db=db))
        assert resp.context["result"]["state"] == "success"
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["toast"]["kind"] == "success"
        assert get_imap_login(db) == "user@ukr.net"


def test_save_imap_settings_error_surfaces_reason_toast(monkeypatch):
    engine = _database()
    _fake_template_response(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        req = _imap_request(
            admin.id, {"imap_login": "user@ukr.net", "imap_password": "bad"}
        )
        with patch("app.routers.settings.MailBox") as mock_mailbox_cls:
            mock_mailbox_cls.return_value.login.side_effect = Exception(
                "AUTHENTICATIONFAILED raw server detail"
            )
            resp = asyncio.run(settings_router_mod.save_imap_settings(request=req, db=db))
        assert resp.context["result"]["state"] == "error"
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["toast"]["kind"] == "error"
        assert "AUTHENTICATIONFAILED" not in trigger["toast"]["message"]


def test_save_imap_settings_blank_password_keeps_saved(monkeypatch):
    engine = _database()
    _fake_template_response(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "imap_login", "old@ukr.net")
        set_setting(db, "imap_password", "keep-me")
        db.commit()
        req = _imap_request(
            admin.id, {"imap_login": "new@ukr.net", "imap_password": ""}
        )
        with patch("app.routers.settings.MailBox") as mock_mailbox_cls:
            mock_mailbox_cls.return_value.login.return_value = MagicMock()
            asyncio.run(settings_router_mod.save_imap_settings(request=req, db=db))
        assert get_imap_login(db) == "new@ukr.net"
        assert get_imap_password(db) == "keep-me"


# --- POST /settings/test-sheets route (Google read-only access probe) ------
#
# Read-only access check for the settings "Майстер" Google step. Like the two
# routes above it does blocking network I/O (open_spreadsheet), so it is a
# plain `def` and is called directly here.


def test_test_sheets_connection_requires_authentication():
    engine = _database()
    with Session(engine) as db, pytest.raises(HTTPException) as exc:
        settings_router_mod.test_sheets_connection(request=_request(None), db=db)
    assert exc.value.status_code == 401


def test_test_sheets_connection_requires_admin_role():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.test_sheets_connection(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


def test_test_sheets_connection_requires_loopback():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.test_sheets_connection(
                request=_request(admin.id, host="203.0.113.5"), db=db
            )
    assert exc.value.status_code == 403


def test_test_sheets_connection_reports_error_when_not_configured(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        context = settings_router_mod.test_sheets_connection(request=_request(admin.id), db=db)
    assert context["result"]["state"] == "error"
    assert "збережіть" in context["result"]["message"]


def test_test_sheets_connection_reports_success_on_access(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "google_sheet_id", "sheet-123")
        set_setting(db, "google_service_account_json", '{"type": "service_account"}')
        db.commit()

        with patch("app.routers.settings.open_spreadsheet") as mock_open:
            mock_open.return_value.worksheets.return_value = [MagicMock()]
            context = settings_router_mod.test_sheets_connection(request=_request(admin.id), db=db)

    assert context["result"]["state"] == "success"


def test_test_sheets_connection_reports_safe_error_on_failure(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "google_sheet_id", "sheet-123")
        set_setting(db, "google_service_account_json", '{"type": "service_account"}')
        db.commit()

        with patch("app.routers.settings.open_spreadsheet") as mock_open:
            mock_open.side_effect = Exception("PermissionDenied raw google detail")
            context = settings_router_mod.test_sheets_connection(request=_request(admin.id), db=db)

    assert context["result"]["state"] == "error"
    # Raw gspread/Google error text must never leak into the UI message.
    assert "PermissionDenied" not in context["result"]["message"]


# --- _sheets_configured — service_account vs oauth mode --------------------


def test_sheets_configured_false_without_sheet_id():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        assert web._sheets_configured(db) is False


def test_sheets_configured_true_for_service_account_mode():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(db, "google_sheet_id", "sheet-1")
        set_setting(db, "google_service_account_json", '{"type": "service_account"}')
        db.commit()
        assert web._sheets_configured(db) is True


def test_sheets_configured_ignores_service_account_json_in_oauth_mode():
    """Switching to oauth mode must not fall back to a stale service-account
    JSON — only the oauth client json + refresh token count."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(db, "google_sheet_id", "sheet-1")
        set_setting(db, "google_service_account_json", '{"type": "service_account"}')
        set_setting(db, "google_auth_mode", "oauth")
        db.commit()
        assert web._sheets_configured(db) is False


def test_sheets_configured_true_for_oauth_mode_with_token():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(db, "google_sheet_id", "sheet-1")
        set_setting(db, "google_auth_mode", "oauth")
        set_setting(db, "google_oauth_client_json", '{"installed": {"client_id": "a", "client_secret": "b"}}')
        set_setting(db, "google_oauth_refresh_token", "rt-123")
        db.commit()
        assert web._sheets_configured(db) is True


# --- POST /settings/google-oauth/start --------------------------------------


def test_start_google_oauth_requires_admin_role():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.start_google_oauth(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


def test_start_google_oauth_requires_loopback():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.start_google_oauth(request=_request(admin.id, host="203.0.113.5"), db=db)
    assert exc.value.status_code == 403


def test_start_google_oauth_reports_error_when_client_json_missing(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        context = settings_router_mod.start_google_oauth(request=_request(admin.id), db=db)
    assert context["result"]["state"] == "error"
    assert "OAuth Client JSON" in context["result"]["message"]


def test_start_google_oauth_success_saves_refresh_token_and_switches_mode(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "google_oauth_client_json", '{"installed": {"client_id": "a", "client_secret": "b"}}')
        db.commit()

        with patch("app.routers.settings.run_authorization_flow", return_value="rt-new-token") as mock_flow, \
             patch("app.routers.settings.reset_sheets_cache") as mock_reset:
            context = settings_router_mod.start_google_oauth(request=_request(admin.id), db=db)

        mock_flow.assert_called_once()
        mock_reset.assert_called_once()
        assert context["result"]["state"] == "success"

        from app.settings_store import get_google_auth_mode, get_google_oauth_refresh_token
        assert get_google_oauth_refresh_token(db) == "rt-new-token"
        assert get_google_auth_mode(db) == "oauth"


def test_start_google_oauth_reports_flow_error_safely(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "google_oauth_client_json", '{"installed": {"client_id": "a", "client_secret": "b"}}')
        db.commit()

        with patch("app.routers.settings.run_authorization_flow", side_effect=OAuthFlowError("Google відхилив авторизацію: access_denied")):
            context = settings_router_mod.start_google_oauth(request=_request(admin.id), db=db)

    assert context["result"]["state"] == "error"
    assert "access_denied" in context["result"]["message"]


def test_start_google_oauth_reports_safe_error_on_unexpected_exception(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "google_oauth_client_json", '{"installed": {"client_id": "a", "client_secret": "b"}}')
        db.commit()

        with patch("app.routers.settings.run_authorization_flow", side_effect=Exception("raw internal detail")):
            context = settings_router_mod.start_google_oauth(request=_request(admin.id), db=db)

    assert context["result"]["state"] == "error"
    assert "raw internal detail" not in context["result"]["message"]


# --- POST /settings/google-oauth/disconnect ---------------------------------


def test_disconnect_google_oauth_requires_admin_role():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.disconnect_google_oauth(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


def test_disconnect_google_oauth_clears_token_and_resets_mode():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        set_setting(db, "google_auth_mode", "oauth")
        set_setting(db, "google_oauth_refresh_token", "rt-old")
        db.commit()

        with patch("app.routers.settings.reset_sheets_cache") as mock_reset:
            resp = settings_router_mod.disconnect_google_oauth(request=_request(admin.id), db=db)

        mock_reset.assert_called_once()
        assert resp.status_code == 303

        from app.settings_store import get_google_auth_mode, get_google_oauth_refresh_token
        assert not get_google_oauth_refresh_token(db)
        assert get_google_auth_mode(db) == "service_account"


# --- Стан системи · self-check stream ---------------------------------------
#
# The endpoint streams NDJSON so the UI's progression is real rather than a
# replay of an already-finished run: a manifest line first, then one line per
# probe as it settles, then a final summary. These assert the contract the
# client (settings_console.js) parses, plus the per-probe deadline that keeps a
# half-open socket from wedging the whole run.


def _drain(response) -> list[dict]:
    """Collect the NDJSON lines a StreamingResponse produces.

    Starlette wraps the sync generator into an async iterator, so this has to
    go through the event loop even though the endpoint itself is sync.
    """
    async def _collect():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        return chunks

    chunks = asyncio.run(_collect())
    return [json.loads(line) for line in "".join(chunks).splitlines() if line.strip()]


def test_selfcheck_streams_manifest_then_result_per_step():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        response = settings_router_mod.settings_selfcheck(request=_request(admin.id), db=db)
        messages = _drain(response)

    assert response.media_type == "application/x-ndjson"
    manifest, *rest = messages
    steps = manifest["steps"]
    assert [s["key"] for s in steps] == [
        "sheets", "imap", "export", "technician", "disk", "backup", "update",
    ]

    *results, summary = rest
    # One result line per manifest entry, same order — the client settles rows
    # positionally, so a mismatch would label results with the wrong names.
    assert [r["key"] for r in results] == [s["key"] for s in steps]
    for result in results:
        assert set(result) == {"key", "name", "ok", "warn", "detail", "ms"}
    assert summary["done"] is True
    assert summary["total"] == len(steps)
    assert summary["passed"] == sum(1 for r in results if r["ok"])


def test_selfcheck_reports_unconfigured_rather_than_crashing():
    """Nothing is configured in a fresh DB: those probes must come back as
    honest failures with a reason, not exceptions."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        messages = _drain(settings_router_mod.settings_selfcheck(request=_request(admin.id), db=db))

    results = {m["key"]: m for m in messages if "ok" in m}
    assert results["sheets"]["ok"] is False
    assert "не налаштовано" in results["sheets"]["detail"]
    assert results["imap"]["ok"] is False
    assert results["export"]["ok"] is False
    # Either reason is honest here: get_export_folder_path falls back to a
    # default path that doesn't exist on a test machine, so "не задано" and
    # "не знайдено" are both correct outcomes for an unconfigured install.
    assert results["export"]["detail"] in {
        "шлях не задано",
        "папку не знайдено за вказаним шляхом",
    }


def test_selfcheck_abandons_a_probe_that_exceeds_the_deadline(monkeypatch):
    """A hung probe is reported as a failure and the run continues — the whole
    point of SELFCHECK_STEP_DEADLINE_SECONDS (mirrors the mail-sync watchdog)."""
    import threading

    release = threading.Event()
    monkeypatch.setattr(settings_router_mod, "SELFCHECK_STEP_DEADLINE_SECONDS", 0.2)

    def _hang(*args, **kwargs):
        release.wait(10)
        return {"state": "success", "message": "занадто пізно"}

    monkeypatch.setattr(settings_router_mod, "_probe_imap_login", _hang)

    engine = _database()
    try:
        with Session(engine, expire_on_commit=False) as db:
            admin = _admin(db)
            set_setting(db, "imap_login", "lab@ukr.net")
            set_setting(db, "imap_password", "secret")
            db.commit()
            messages = _drain(settings_router_mod.settings_selfcheck(request=_request(admin.id), db=db))
    finally:
        release.set()

    results = {m["key"]: m for m in messages if "ok" in m}
    assert results["imap"]["ok"] is False
    assert "немає відповіді" in results["imap"]["detail"]
    # The steps after the wedged one still ran.
    assert results["disk"]["key"] == "disk"
    assert messages[-1]["done"] is True


def test_selfcheck_rejects_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.settings_selfcheck(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403
