"""Тека для файлів з листів — у налаштуваннях (власник 25.09.26).

Досі тека спулу була сталою в коді. Тепер адмін задає її в «Скачування
вкладень»; порожньо — стандартна тека програми. Файли, скачані ДО зміни,
лишаються в старій теці (їхні шляхи збережені), тож стара тека мусить
лишатися довіреною — інакше прев'ю STL і «Відкрити папку» на старих листах
зламались би.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.settings_store as settings_store_mod
from app.models import AppSetting
from app.services.config_state import mail_trusted_roots
from app.settings_store import (
    MAIL_SPOOL_PREV_KEY,
    get_mail_attachments_path,
    get_setting,
    mail_spool_root_map,
    set_setting,
)
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def test_default_when_unset_and_setting_wins(app_db, monkeypatch, tmp_path):  # noqa: F811
    _, session_factory = app_db
    monkeypatch.setattr(settings_store_mod, "MAIL_ATTACHMENTS_PATH", tmp_path / "default")
    with session_factory() as db:
        assert get_mail_attachments_path(db) == str(tmp_path / "default")
        assert mail_spool_root_map(db) == {"mail": str(tmp_path / "default")}
        set_setting(db, "mail_attachments_path", str(tmp_path / "custom"))
        db.commit()
        assert get_mail_attachments_path(db) == str(tmp_path / "custom")
        # Стандартна тека лишається довіреною: там файли, скачані до зміни.
        roots = mail_spool_root_map(db)
        assert roots == {"mail": str(tmp_path / "custom"), "mail_default": str(tmp_path / "default")}
        trusted = {str(p) for p in mail_trusted_roots(db)}
        assert {str(tmp_path / "custom"), str(tmp_path / "default")} <= trusted


def test_saving_a_new_spool_path_remembers_the_previous_one(app_db, monkeypatch, tmp_path):  # noqa: F811
    app, session_factory = app_db
    monkeypatch.setattr(settings_store_mod, "MAIL_ATTACHMENTS_PATH", tmp_path / "default")
    with session_factory() as db:
        set_setting(db, "mail_attachments_path", str(tmp_path / "a"))
        db.commit()
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, _ = client.post("/settings", {"mail_attachments_path": str(tmp_path / "b")})
    assert status in (200, 204, 303)
    with session_factory() as db:
        assert get_mail_attachments_path(db) == str(tmp_path / "b")
        assert get_setting(db, MAIL_SPOOL_PREV_KEY) == str(tmp_path / "a")
        assert mail_spool_root_map(db) == {
            "mail": str(tmp_path / "b"),
            "mail_default": str(tmp_path / "default"),
            "mail_prev": str(tmp_path / "a"),
        }


def test_other_settings_form_does_not_touch_the_spool_path(app_db, tmp_path):  # noqa: F811
    """Форма іншого розділу не несе поля — тека лишається (урок 03.09.26)."""
    app, session_factory = app_db
    with session_factory() as db:
        set_setting(db, "mail_attachments_path", str(tmp_path / "a"))
        db.commit()
    client = MiniClient(app)
    client.login(*ADMIN)
    client.post("/settings", {"export_folder_path": str(tmp_path / "export")})
    with session_factory() as db:
        assert get_mail_attachments_path(db) == str(tmp_path / "a")
        assert db.query(AppSetting).filter_by(key=MAIL_SPOOL_PREV_KEY).count() == 0


def test_check_path_spool_kind_checks_the_current_folder(app_db, monkeypatch, tmp_path):  # noqa: F811
    app, _ = app_db
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(settings_store_mod, "MAIL_ATTACHMENTS_PATH", spool)
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, body = client.post("/settings/check-path", {"kind": "spool", "mail_attachments_path": ""})
    html = body.decode("utf-8") if isinstance(body, bytes) else body
    assert status == 200
    assert "не задано" not in html.lower()
    assert Path(spool).exists()


def test_open_spool_folder_opens_the_current_folder(app_db, monkeypatch, tmp_path):  # noqa: F811
    from app.routers.settings import materials as materials_mod

    app, _ = app_db
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(settings_store_mod, "MAIL_ATTACHMENTS_PATH", spool)
    opened = []
    monkeypatch.setattr(materials_mod, "open_folder_in_explorer", lambda path: opened.append(Path(path)))
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, _ = client.post("/settings/mail-spool/open-folder", {})
    assert status == 200
    assert opened == [spool]


def test_open_spool_folder_404_when_missing(app_db, monkeypatch, tmp_path):  # noqa: F811
    app, _ = app_db
    monkeypatch.setattr(settings_store_mod, "MAIL_ATTACHMENTS_PATH", tmp_path / "nope")
    client = MiniClient(app)
    client.login(*ADMIN)
    status, _, _ = client.post("/settings/mail-spool/open-folder", {})
    assert status == 404
