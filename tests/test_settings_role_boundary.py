"""Operator vs admin boundary on /settings and friends.

Google Sheets / IMAP credentials, sync, user management, and backup stay
admin-only. The two filesystem paths (export_folder_path,
technician_files_path — see app.settings_store.OPERATOR_EDITABLE_KEYS) are
open to any logged-in operator: they're a per-machine detail, not a secret,
and whoever is at the workstation needs to fix a moved folder without
waiting on an admin.

Every boundary is asserted at the ROUTE level (calling the handler directly,
same convention as test_settings_routes.py), because settings.html hiding a
card is not a security control by itself — the server must refuse a
hand-crafted request the same way regardless of what the DOM shows.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers import settings as settings_router_mod
from app.routers import queue as queue_router_mod
from app.db import Base
from app.models import User
from app.settings_store import get_setting


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _admin(db: Session) -> User:
    user = User(username="admin", password_hash="unused", full_name="Адмін", role="адмін")
    db.add(user)
    db.commit()
    return user


def _operator(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Оператор", role="оператор")
    db.add(user)
    db.commit()
    return user


def _form_request(user_id: int, form: dict, host: str = "127.0.0.1", headers: dict | None = None):
    """A fake Request whose `await .form()` returns `form`, matching what
    `post_settings`/`export_backup`/`import_backup` actually call.

    `headers` defaults to empty, i.e. a plain (non-HTMX) browser POST — the
    path that still redirects. Pass {"HX-Request": "true"} to exercise the
    HTMX branch that answers 204 + toast instead.
    """
    async def _form():
        return form

    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host=host),
        form=_form,
        headers=Headers(headers or {}),
    )


# --- GET /settings: reachable by any logged-in role now -------------------


def test_get_settings_allows_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        response = settings_router_mod.get_settings(request=SimpleNamespace(session={"user_id": operator.id}), db=db)
    # A real TemplateResponse (not a redirect/exception) — status 200 by default.
    assert getattr(response, "status_code", 200) == 200


def test_get_settings_hides_operator_list_from_non_admin(monkeypatch):
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        context = settings_router_mod.get_settings(request=SimpleNamespace(session={"user_id": operator.id}), db=db)
    assert context["operators"] == []


def test_get_settings_still_requires_login():
    engine = _database()
    with Session(engine) as db:
        response = settings_router_mod.get_settings(request=SimpleNamespace(session={}), db=db)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- POST /settings: operator may only touch path fields -------------------


def test_operator_can_save_path_fields():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(
            operator.id,
            {"action": "save", "export_folder_path": r"D:\export", "technician_files_path": r"D:\tech"},
        )
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert response.status_code == 303
    assert get_setting(db, "export_folder_path") == r"D:\export"
    assert get_setting(db, "technician_files_path") == r"D:\tech"


def test_operator_cannot_set_credentials_even_when_posted():
    """Field-level enforcement: even if a hand-crafted request includes a
    restricted key, it must be silently dropped, not applied."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(
            operator.id,
            {
                "action": "save",
                "export_folder_path": r"D:\export",
                "google_sheet_id": "attacker-supplied-sheet-id",
                "imap_password": "attacker-supplied-password",
            },
        )
        asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert get_setting(db, "export_folder_path") == r"D:\export"
    assert get_setting(db, "google_sheet_id") is None
    assert get_setting(db, "imap_password") is None


def test_operator_cannot_trigger_sync_even_when_requested():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {"action": "save_and_sync", "export_folder_path": r"D:\export"})
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    # Falls back to a plain save-and-redirect-to-/settings, never the
    # sync branch's redirect to "/".
    assert response.headers["location"] == "/settings?saved=1"


def test_admin_can_still_set_everything():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        request = _form_request(
            admin.id,
            {
                "action": "save",
                "google_sheet_id": "real-sheet-id",
                "imap_login": "lab@ukr.net",
                "export_folder_path": r"D:\export",
            },
        )
        asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert get_setting(db, "google_sheet_id") == "real-sheet-id"
    assert get_setting(db, "imap_login") == "lab@ukr.net"
    assert get_setting(db, "export_folder_path") == r"D:\export"


def test_post_settings_still_requires_login():
    engine = _database()
    with Session(engine) as db:
        request = _form_request(None, {})
        request.session = {}
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Admin-only routes stay admin-only (regression) -------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda request, db: queue_router_mod.sync_sheets(request=request, db=db),
        lambda request, db: settings_router_mod.test_imap_connection(request=request, db=db),
        lambda request, db: asyncio.run(settings_router_mod.create_operator(request=request, db=db)),
        lambda request, db: settings_router_mod.export_backup(
            request=request, backup_password="pw123456", backup_password_confirm="pw123456", db=db
        ),
    ],
)
def test_admin_only_routes_still_reject_operator(call):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {})
        with pytest.raises(HTTPException) as exc:
            call(request, db)
    assert exc.value.status_code == 403


def test_operator_cannot_import_backup():
    from fastapi import UploadFile
    import io

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {})
        upload = UploadFile(filename="backup.json", file=io.BytesIO(b"{}"))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                settings_router_mod.import_backup(
                    request=request,
                    backup_password="whatever",
                    confirm_replace="on",
                    backup_file=upload,
                    db=db,
                )
            )
    assert exc.value.status_code == 403


# --- HTMX save keeps the operator on the edited section -------------------
#
# The plain POST redirects to /settings?saved=1 — no #hash — which under the
# console layout lands on «Стан системи» instead of the form just saved. The
# HTMX branch must answer 204 (nothing to swap) plus a toast instead, and the
# non-HTMX branch must still redirect for the no-JS case.


def test_htmx_save_answers_204_with_toast_instead_of_redirect():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(
            operator.id,
            {"action": "save", "export_folder_path": r"C:\export"},
            headers={"HX-Request": "true"},
        )
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))

    assert response.status_code == 204
    assert "HX-Trigger" in response.headers
    payload = json.loads(response.headers["HX-Trigger"])
    assert payload["toast"]["kind"] == "success"
    # The value still had to be saved — 204 must not mean "did nothing".
    with Session(engine, expire_on_commit=False) as db:
        assert get_setting(db, "export_folder_path") == r"C:\export"


def test_plain_post_still_redirects_for_no_js():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        request = _form_request(operator.id, {"action": "save", "export_folder_path": r"C:\export"})
        response = asyncio.run(settings_router_mod.post_settings(request=request, db=db))

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"
