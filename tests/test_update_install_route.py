"""POST /settings/update/install: admin+loopback gates (same boundary as
every other filesystem/process-touching settings action — see
tests/test_settings_role_boundary.py and is_loopback_request), plus
the "no update known" and "starts a background thread" branches. The
download/verify/install itself is app/update_check.py's own responsibility
and is tested there; here we only check that install_update wires it up
and never blocks the request on it.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

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
        response = settings_router_mod.install_update(request=_request(None), db=db)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_rejects_operator():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.install_update(request=_request(operator.id), db=db)
    assert exc.value.status_code == 403


def test_rejects_non_loopback_admin():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.install_update(request=_request(admin.id, host="203.0.113.5"), db=db)
    assert exc.value.status_code == 403


def test_no_known_update_flashes_and_redirects_without_starting_thread():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        request = _request(admin.id)
        with patch("app.routers.settings.get_known_update", return_value=None), patch("app.routers.settings.Thread") as mock_thread:
            response = settings_router_mod.install_update(request=request, db=db)
    mock_thread.assert_not_called()
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert request.session["settings_flash"]["kind"] == "error"


def test_known_update_starts_background_thread_and_flashes_success():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        request = _request(admin.id)
        with patch("app.routers.settings.get_known_update", return_value=_RELEASE), patch("app.routers.settings.Thread") as mock_thread:
            response = settings_router_mod.install_update(request=request, db=db)
    mock_thread.assert_called_once()
    _, kwargs = mock_thread.call_args
    assert kwargs["args"] == (_RELEASE,)
    mock_thread.return_value.start.assert_called_once()
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert request.session["settings_flash"]["kind"] == "success"


# --- _install_update_in_background: never raises out of the thread --------


def test_install_in_background_download_failure_is_swallowed():
    with patch("app.routers.settings.download_and_verify", side_effect=Exception("boom")):
        settings_router_mod._install_update_in_background(_RELEASE)  # must not raise


def test_install_in_background_calls_download_then_launch():
    with patch("app.routers.settings.download_and_verify", return_value="C:/fake/installer.exe") as mock_download, patch(
        "app.routers.settings.launch_silent_install"
    ) as mock_launch:
        settings_router_mod._install_update_in_background(_RELEASE)
    # progress= — колбек для оверлею (04.09.26); реліз лишається єдиним позиційним.
    assert mock_download.call_count == 1
    assert mock_download.call_args.args == (_RELEASE,)
    assert callable(mock_download.call_args.kwargs.get("progress"))
    mock_launch.assert_called_once_with("C:/fake/installer.exe")


# --- стан встановлення для оверлею -----------------------------------------


def test_background_install_reports_progress_then_launched(monkeypatch):
    """Оверлей читає стан з пам'яті: скачування з байтами → запуск → launched."""
    from app import update_check as uc

    uc.reset_install_state_for_tests()
    seen = []

    def fake_download(release, progress=None, **_):
        progress(5, 10)
        seen.append(uc.install_state())
        progress(10, 10)
        return "C:/fake/installer.exe"

    monkeypatch.setattr(settings_router_mod, "download_and_verify", fake_download)
    monkeypatch.setattr(settings_router_mod, "launch_silent_install", lambda path: seen.append(uc.install_state()))
    settings_router_mod._install_update_in_background(_RELEASE)

    assert seen[0] == {"stage": "downloading", "version": "9.9.9", "done": 5, "total": 10}
    assert seen[1]["stage"] == "launching"
    assert uc.install_state() == {"stage": "launched", "version": "9.9.9"}
    uc.reset_install_state_for_tests()


def test_background_install_failure_is_visible_to_overlay(monkeypatch):
    """Без цього оверлей після невдалого скачування висів би вічно."""
    import requests as rq
    from app import update_check as uc

    uc.reset_install_state_for_tests()

    def fake_download(release, progress=None, **_):
        raise rq.exceptions.ReadTimeout("HTTPSConnectionPool(host='github.com'): Read timed out")

    monkeypatch.setattr(settings_router_mod, "download_and_verify", fake_download)
    settings_router_mod._install_update_in_background(_RELEASE)
    state = uc.install_state()
    assert state["stage"] == "failed" and state["version"] == "9.9.9"
    assert "обірвалось" in state["message"] and "HTTPSConnectionPool" not in state["message"]
    uc.reset_install_state_for_tests()


def test_status_route_gates_like_install():
    from app import update_check as uc

    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.update_install_status(request=_request(None), db=db)
        assert exc.value.status_code == 401

        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.update_install_status(request=_request(operator.id), db=db)
        assert exc.value.status_code == 403

        admin = _admin(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.update_install_status(request=_request(admin.id, host="192.168.1.5"), db=db)
        assert exc.value.status_code == 403

        uc.set_install_state(stage="downloading", version="9.9.9", done=1, total=2)
        response = settings_router_mod.update_install_status(request=_request(admin.id), db=db)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert b'"stage":"downloading"' in response.body
        uc.reset_install_state_for_tests()
