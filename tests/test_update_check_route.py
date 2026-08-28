"""POST /settings/update/check: admin+loopback gates (same boundary as every
other update/settings action — see tests/test_update_install_route.py), and the
two result branches (newer version found vs up to date). The GitHub probe itself
is app/update_check.py's responsibility and is mocked here; this only checks that
check_update runs the one-shot tick and hands the right context to the fragment.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers import settings as settings_router_mod
from app.db import Base
from app.models import User
from app.update_check import ReleaseInfo


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


def _request(user_id: int | None, host: str = "127.0.0.1"):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, client=SimpleNamespace(host=host))


_RELEASE = ReleaseInfo(
    version="9.9.9",
    html_url="https://example/release",
    installer_url="https://example/installer.exe",
    checksum_url="https://example/installer.sha256",
    notes="",
)


def test_requires_login():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.check_update(request=_request(None), db=db)
    assert exc.value.status_code == 401


def test_rejects_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.check_update(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


def test_rejects_non_loopback_admin():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.check_update(request=_request(admin.id, host="203.0.113.5"), db=db)
    assert exc.value.status_code == 403


def test_runs_tick_and_reports_newer_version(monkeypatch):
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with patch("app.routers.settings._update_check_tick") as tick, patch(
            "app.routers.settings.get_known_update", return_value=_RELEASE
        ):
            context = settings_router_mod.check_update(request=_request(admin.id), db=db)
    tick.assert_called_once()
    assert context["release"] is _RELEASE
    assert context["current_version"] == web.VERSION


def test_reports_up_to_date_when_no_release(monkeypatch):
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with patch("app.routers.settings._update_check_tick"), patch(
            "app.routers.settings.get_known_update", return_value=None
        ):
            context = settings_router_mod.check_update(request=_request(admin.id), db=db)
    assert context["release"] is None
    assert context["current_version"] == web.VERSION
