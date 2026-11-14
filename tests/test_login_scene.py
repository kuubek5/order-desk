"""Сцена екрана входу на вибір адміна (`login_scene`, власник 14.11.26)."""

from __future__ import annotations

import pytest

from app.services import login_scene
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура


def test_default_is_the_video_and_unknown_value_falls_back(app_db):  # noqa: F811
    _app, factory = app_db
    with factory() as db:
        assert login_scene.current(db) == "video"
        with pytest.raises(ValueError):
            login_scene.save(db, "disco")
        assert login_scene.current(db) == "video"
    assert login_scene.normalize("FREZA ") == "freza"
    assert login_scene.normalize("../etc") == "video" and login_scene.normalize(None) == "video"


def test_login_page_is_unchanged_until_admin_picks_a_scene(app_db):  # noqa: F811
    app, _factory = app_db
    html = MiniClient(app).get("/login")[2]
    assert "ls-video" in html and "login_scenes.js" not in html and "ls-live" not in html
    assert 'action="/login"' in html and 'id="password"' in html


@pytest.mark.parametrize("scene, marker", [
    ("freza", '<canvas class="ls-canvas">'), ("tochky", '<canvas class="ls-canvas">'),
    ("svitlo", '<canvas class="ls-gl">'), ("pult", 'class="lsp-name"'),
])
def test_each_scene_renders_its_stage_and_keeps_the_form(app_db, scene, marker):  # noqa: F811
    app, factory = app_db
    admin = MiniClient(app)
    admin.login(*ADMIN)
    assert admin.post("/settings/login-scene", {"scene": scene})[0] == 303
    with factory() as db:
        assert login_scene.current(db) == scene
    html = MiniClient(app).get("/login")[2]
    assert f'data-scene="{scene}"' in html and marker in html and "login_scenes.js" in html
    assert "ls-video" not in html, "відео не вантажиться разом зі сценою"
    assert 'action="/login"' in html and 'id="username"' in html and 'id="password"' in html
    assert 'id="reg-slot"' in html, "прихована реєстрація лишається"
    assert "data-preview" not in html and "ls-preview-back" not in html


def test_scene_survives_a_wrong_password(app_db):  # noqa: F811
    app, _factory = app_db
    admin = MiniClient(app)
    admin.login(*ADMIN)
    admin.post("/settings/login-scene", {"scene": "freza"})
    status, _headers, html = MiniClient(app).post("/login", {"username": ADMIN[0], "password": "не той"})
    assert status in (200, 401) and "Невірний логін або пароль" in html
    assert 'data-scene="freza"' in html, "після помилки сцена та сама, а не відео"


def test_admin_can_log_in_with_a_scene_on(app_db):  # noqa: F811
    app, _factory = app_db
    admin = MiniClient(app)
    admin.login(*ADMIN)
    admin.post("/settings/login-scene", {"scene": "pult"})
    fresh = MiniClient(app)
    fresh.login(*ADMIN)
    assert fresh.get("/settings")[0] == 200


def test_settings_section_preview_and_operator_cannot_change(app_db):  # noqa: F811
    app, factory = app_db
    admin = MiniClient(app)
    admin.login(*ADMIN)
    settings = admin.get("/settings")[2]
    assert 'data-sec="login-scene"' in settings and settings.count('name="scene"') == len(login_scene.SCENES)
    assert admin.post("/settings/login-scene", {"scene": "disco"})[0] == 303
    with factory() as db:
        assert login_scene.current(db) == "video", "невідома сцена не зберігається"

    status, _headers, preview = admin.get("/settings/login-scene/preview?scene=svitlo")
    assert status == 200 and 'data-scene="svitlo"' in preview and 'data-preview="1"' in preview
    assert "ls-preview-back" in preview and "preventDefault" in preview
    with factory() as db:
        assert login_scene.current(db) == "video", "перегляд нічого не зберігає"

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert 'data-sec="login-scene"' not in operator.get("/settings")[2]
    assert operator.post("/settings/login-scene", {"scene": "freza"})[0] == 403
    assert operator.get("/settings/login-scene/preview?scene=freza")[0] == 403
    assert MiniClient(app).get("/settings/login-scene/preview?scene=freza")[0] in (303, 401, 403)
    with factory() as db:
        assert login_scene.current(db) == "video"
