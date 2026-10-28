"""Фон порожньої черги — вибір оператора (28.10.26).

Стережемо:
1. «за темою» (порожнє в БД) дає кадр своєї теми; стара тема "" = forge;
2. невідоме значення = «за темою», а не «без фону»;
3. роут приймає `auto` / ключ / `none`, чуже — 422; `auto` лягає порожнім;
4. порожня черга малює обраний кадр, «без фону» — схований хост без src;
5. шестерня показує рядок вибору з підсвіченим поточним.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import User
from app.services import queue_empty_bg as qeb
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def test_every_background_has_its_files():
    for bg in qeb.BACKGROUNDS:
        assert (STATIC / bg.file).is_file(), bg.file
        assert (STATIC / bg.thumb).is_file(), bg.thumb


def test_auto_follows_the_theme():
    assert qeb.resolve("", "aurora").key == "aurora-1"
    assert qeb.resolve("", "forge").key == "forge-2"
    assert qeb.resolve("", "teal").key == "teal-2"
    # Стара БД (тема "") і невідома тема — як forge, той самий дефолт, що в ui_prefs.
    assert qeb.resolve("", "").key == "forge-2"
    assert qeb.resolve(None, "щось-нове").key == "forge-2"


def test_explicit_choice_ignores_the_theme_and_none_hides():
    assert qeb.resolve("photo", "aurora").key == "photo"
    assert qeb.resolve(qeb.NONE, "aurora") is None


def test_unknown_value_falls_back_to_theme_not_to_nothing():
    assert qeb.resolve("прибраний-кадр", "aurora").key == "aurora-1"
    assert qeb.choice_of("прибраний-кадр") == qeb.AUTO


def _set_theme(session_factory, theme: str) -> None:
    with session_factory() as db:
        user = db.query(User).filter_by(username=ADMIN[0]).one()
        user.ui_theme = theme
        db.commit()


def _stored(session_factory) -> str:
    with session_factory() as db:
        return db.query(User).filter_by(username=ADMIN[0]).one().queue_empty_bg


def test_route_saves_choice_and_auto_is_stored_empty(app_db):  # noqa: F811
    app, session_factory = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    assert client.post("/account/queue-empty-bg", {"bg": "teal-1"})[0] == 204
    assert _stored(session_factory) == "teal-1"
    assert client.post("/account/queue-empty-bg", {"bg": "none"})[0] == 204
    assert _stored(session_factory) == "none"
    assert client.post("/account/queue-empty-bg", {"bg": "auto"})[0] == 204
    assert _stored(session_factory) == ""
    # Чуже значення летить у src — не зберігаємо.
    assert client.post("/account/queue-empty-bg", {"bg": "../../etc"})[0] == 422
    assert client.post("/account/queue-empty-bg", {"bg": ""})[0] == 422
    assert _stored(session_factory) == ""


def test_route_needs_login(app_db):  # noqa: F811
    app, _ = app_db
    assert MiniClient(app).post("/account/queue-empty-bg", {"bg": "photo"})[0] == 401


def test_empty_queue_draws_the_chosen_frame(app_db):  # noqa: F811
    app, session_factory = app_db
    _set_theme(session_factory, "aurora")
    client = MiniClient(app)
    client.login(*ADMIN)

    status, _, html = client.get("/?ready=all&partial=rows")
    assert status == 200, html[:400]
    assert "queue-empty-live" in html, "очікувалась порожня черга"
    assert "/static/img/queue-bg/aurora-1.jpg" in html, "за темою «Аврора» — кадр Аврори"

    client.post("/account/queue-empty-bg", {"bg": "photo"})
    html = client.get("/?ready=all&partial=rows")[2]
    assert "/static/video/queue-empty-poster.jpg" in html
    assert "aurora-1.jpg" not in html

    client.post("/account/queue-empty-bg", {"bg": "none"})
    html = client.get("/?ready=all&partial=rows")[2]
    host = html[html.index("data-empty-bg-host"):]
    host = host[: host.index("</span>")]
    assert "hidden" in host
    # Без src зовсім: src="" змусив би браузер запитати саму сторінку.
    assert "src=" not in host


def test_gear_offers_backgrounds_with_current_pressed(app_db):  # noqa: F811
    app, session_factory = app_db
    client = MiniClient(app)
    client.login(*ADMIN)
    client.post("/account/queue-empty-bg", {"bg": "forge-1"})
    html = client.get("/?ready=all")[2]
    assert "Фон порожньої черги" in html
    for bg in qeb.BACKGROUNDS:
        assert f'data-empty-bg="{bg.key}"' in html
    start = html.index('data-empty-bg="forge-1"')
    assert 'aria-pressed="true"' in html[start: start + 300]
    start = html.index('data-empty-bg="auto"')
    assert 'aria-pressed="false"' in html[start: start + 400]
