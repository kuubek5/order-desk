"""Екран «Невідомі екрани» (`app/routers/screens.py` + `app/services/screen_inbox.py`).

Дошка + фрагменти рендеряться через справжній ASGI-застосунок
(`tests/asgi_client.MiniClient`) — так само, як `tests/test_settings_slabs_render.py`
доводить, що плита стану доходить до HTML, а не лишається правдою лише в
контексті роута. Картинки (`frame.png`/`zone.png`) перевіряються прямим
викликом роута: там цікавить не HTML, а те, що шлях до файлу побудований з
РЯДКА бази (`screen_inbox.image_path`), а не зі значення в адресі.

`screen_inbox.DATA_DIR` — власне імʼя модуля (`from app.config import
DATA_DIR`), тому патчити варто саме його: підміна `app.config.DATA_DIR` була б
тихим no-op (CLAUDE.md §14, «після переносу коду підміна мовчки стає no-op»).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import Base
from app.models import ScreenPuzzle, User
from app.routers import screens as screens_router_mod
from app.services import screen_inbox
from tests.asgi_client import MiniClient

# Скринька — адмінський екран (рішення власника 15.09.26), тож «свій»
# користувач у тестах функціональності саме адмін. Оператор лишається поруч
# рівно для перевірок відмови.
USER = ("screensadmin", "Scr33n-Adm-1")
OPERATOR = ("screensop", "Scr33n-Op-1")


@pytest.fixture(autouse=True)
def _screen_inbox_tmp(monkeypatch, tmp_path):
    """Файли скриньки — у tmp_path, не в справжній `export`/`DATA_DIR`.

    `screen_inbox` тримає `DATA_DIR` власною назвою в модулі, тож
    `monkeypatch.setattr(app.config, "DATA_DIR", ...)` сюди б не долетів.
    """
    monkeypatch.setattr(screen_inbox, "DATA_DIR", tmp_path)
    screen_inbox.reset_state_for_tests()
    yield
    screen_inbox.reset_state_for_tests()


def _frame(color=(10, 10, 10)):
    return Image.new("RGB", (640, 480), color)


def _note(db, *, color=(10, 10, 10), reason="layout_unknown", name="Піч 3",
          key="furnace-3", kind=None, zone=True, now=None):
    """Завести загадку через СПРАВЖНІЙ шлях запису (`screen_inbox.note`), а не
    вставкою рядка напряму — інакше тест не перевіряє власне запис файлів."""
    kind = kind or screen_inbox.KIND_FURNACE
    zone_crop = Image.new("RGB", (40, 20), color) if zone else None
    puzzle_id = screen_inbox.note(
        db, kind=kind, key=key, name=name, frame=_frame(color),
        reason=reason, detail="", zone_crop=zone_crop, now=now,
    )
    assert puzzle_id is not None, "note() мала завести новий рядок"
    return puzzle_id


# ── Повний застосунок (сторінка, підпис, «неважливо») ──────────────────────


@pytest.fixture
def app_db(monkeypatch):
    """Застосунок на порожній базі в памʼяті + ліцензія — той самий рецепт,
    що в `tests/test_settings_slabs_render.py` (license_gate інакше не пускає
    далі за /license)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import app.web as web
    from app import license as license_module
    from app.routers import deps
    from app.settings_store import set_setting

    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)

    def session_factory():
        return Session(engine, expire_on_commit=False)

    monkeypatch.setattr(deps, "SessionLocal", session_factory)
    monkeypatch.setattr(web, "SessionLocal", session_factory, raising=False)

    private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        license_module,
        "_PUBLIC_KEY_BYTES",
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    with session_factory() as db:
        set_setting(
            db,
            "license_key",
            license_module.encode_license_key(
                {
                    "machine_id": license_module.get_machine_id(),
                    "customer": "tests",
                    "issued_at": "2026-01-01T00:00:00",
                    "expires_at": "2099-01-01T00:00:00",
                },
                private,
            ),
        )
        db.add(
            User(
                username=USER[0],
                password_hash=hash_password(USER[1]),
                full_name="Адмін",
                role="адмін",
            )
        )
        db.add(
            User(
                username=OPERATOR[0],
                password_hash=hash_password(OPERATOR[1]),
                full_name="Оператор",
                role="оператор",
            )
        )
        db.commit()

    return web.app, session_factory


def _login(app) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*USER)
    assert status in (200, 302, 303), status
    return client


def test_screens_page_shows_device_reason_and_seen_count(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        puzzle_id = _note(db, name="Піч 3", reason="layout_unknown")

    client = _login(app)
    status, _, html = client.get("/screens")
    assert status == 200

    assert "Піч 3" in html
    assert screen_inbox.REASONS["layout_unknown"] in html
    # бачено 1 раз — рядок _screen_inbox_row.html, множина/однина рахується
    # окремо, тут одна загадка = «1 раз».
    assert 'бачено <b class="sinb-mono">1</b> раз' in html
    # Контракт з шапки screens.html: підпис нічого не вмикає.
    assert "Підпис нічого не вмикає й не вимикає." in html
    assert f'id="sinb-row-{puzzle_id}"' in html


def test_screens_page_without_login_redirects_not_500_not_200(app_db):
    app, _ = app_db
    client = MiniClient(app)  # без /login
    status, headers, _ = client.get("/screens")
    assert status == 303, status
    assert headers.get("location") == "/login"


def test_label_route_stores_label_and_returns_fragment_with_oob_counts(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        puzzle_id = _note(db)

    client = _login(app)
    status, _, fragment = client.post(
        f"/screens/{puzzle_id}/label", {"label": "екран помилки після відкриття", "show": "0"}
    )
    assert status == 200, status

    with session_factory() as db:
        row = db.get(ScreenPuzzle, puzzle_id)
        assert row.label == "екран помилки після відкриття"
        assert row.labeled_at is not None
        assert row.labeled_by_id is not None

    assert "екран помилки після відкриття" in fragment
    assert 'id="sinb-counts"' in fragment and 'hx-swap-oob="true"' in fragment
    # Тост підтверджує, що застосунок нічого не «вмикає» цим збереженням.
    assert "Застосунок від цього нічого не навчився" in fragment


def test_label_route_empty_value_clears_label_fields(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        puzzle_id = _note(db)
        screen_inbox.set_label(db, puzzle_id, "старий підпис", user_id=None)
        row = db.get(ScreenPuzzle, puzzle_id)
        assert row.label == "старий підпис"

    client = _login(app)
    status, _, _ = client.post(f"/screens/{puzzle_id}/label", {"label": "", "show": "0"})
    assert status == 200, status

    with session_factory() as db:
        row = db.get(ScreenPuzzle, puzzle_id)
        assert row.label == ""
        assert row.labeled_at is None
        assert row.labeled_by_id is None


def test_dismiss_hides_row_by_default_and_shows_it_with_query_flag(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        puzzle_id = _note(db, name="Верстат 250i", reason="glyph_unknown")

    client = _login(app)
    status, _, board = client.post(
        f"/screens/{puzzle_id}/dismiss", {"dismissed": "on", "show": "0"}
    )
    assert status == 200, status
    assert f'id="sinb-row-{puzzle_id}"' not in board

    with session_factory() as db:
        assert db.get(ScreenPuzzle, puzzle_id).dismissed is True

    status, _, default_page = client.get("/screens")
    assert f'id="sinb-row-{puzzle_id}"' not in default_page

    status, _, shown_page = client.get("/screens?dismissed=1")
    assert f'id="sinb-row-{puzzle_id}"' in shown_page
    assert "неважливо" in shown_page


def test_dismiss_off_returns_row_to_the_default_board(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        puzzle_id = _note(db)
        screen_inbox.set_dismissed(db, puzzle_id, True)

    client = _login(app)
    status, _, board = client.post(
        f"/screens/{puzzle_id}/dismiss", {"dismissed": "off", "show": "1"}
    )
    assert status == 200, status
    assert f'id="sinb-row-{puzzle_id}"' in board

    with session_factory() as db:
        assert db.get(ScreenPuzzle, puzzle_id).dismissed is False

    status, _, default_page = client.get("/screens")
    assert f'id="sinb-row-{puzzle_id}"' in default_page


# ── Картинки: шлях береться з рядка бази, не з адреси ──────────────────────


def _image_user(db, role: str = "адмін") -> User:
    """Кадри скриньки бачить лише адмін (15.09.26) — типово він і в тестах.
    Оператор створюється тим самим помічником для перевірок відмови."""
    suffix = "adm" if role == "адмін" else "op"
    user = User(
        username=f"img{suffix}", password_hash="unused", full_name="Хто", role=role
    )
    db.add(user)
    db.commit()
    return user


def _image_request(user_id):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session)


@pytest.fixture
def image_db():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db


def test_frame_png_serves_exactly_the_path_image_path_resolves(image_db):
    user = _image_user(image_db)
    puzzle_id = _note(image_db, name="Піч 5")
    puzzle = image_db.get(ScreenPuzzle, puzzle_id)

    response = screens_router_mod.screen_frame(
        request=_image_request(user.id), puzzle_id=puzzle_id, db=image_db
    )
    expected = screen_inbox.image_path(puzzle, "frame")
    assert expected is not None and expected.exists()
    assert str(response.path) == str(expected)
    assert response.media_type == "image/png"


def test_zone_png_serves_exactly_the_path_image_path_resolves(image_db):
    user = _image_user(image_db)
    puzzle_id = _note(image_db, name="Піч 5", zone=True)
    puzzle = image_db.get(ScreenPuzzle, puzzle_id)

    response = screens_router_mod.screen_zone(
        request=_image_request(user.id), puzzle_id=puzzle_id, db=image_db
    )
    expected = screen_inbox.image_path(puzzle, "zone")
    assert expected is not None and expected.exists()
    assert str(response.path) == str(expected)


def test_frame_png_missing_puzzle_is_404(image_db):
    user = _image_user(image_db)
    with pytest.raises(HTTPException) as exc:
        screens_router_mod.screen_frame(
            request=_image_request(user.id), puzzle_id=999999, db=image_db
        )
    assert exc.value.status_code == 404


def test_zone_png_without_a_zone_crop_is_404(image_db):
    user = _image_user(image_db)
    puzzle_id = _note(image_db, name="Без вирізу", zone=False)

    with pytest.raises(HTTPException) as exc:
        screens_router_mod.screen_zone(
            request=_image_request(user.id), puzzle_id=puzzle_id, db=image_db
        )
    assert exc.value.status_code == 404


def test_frame_png_without_session_is_401(image_db):
    puzzle_id = _note(image_db, name="Піч 7")
    with pytest.raises(HTTPException) as exc:
        screens_router_mod.screen_frame(
            request=_image_request(None), puzzle_id=puzzle_id, db=image_db
        )
    assert exc.value.status_code == 401


def test_zone_png_without_session_is_401(image_db):
    puzzle_id = _note(image_db, name="Піч 7", zone=True)
    with pytest.raises(HTTPException) as exc:
        screens_router_mod.screen_zone(
            request=_image_request(None), puzzle_id=puzzle_id, db=image_db
        )
    assert exc.value.status_code == 401


# ── Доступ: лише адміністратор ─────────────────────────────────────────────


def _login_as(app, who) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*who)
    assert status in (200, 302, 303), status
    return client


def test_operator_gets_403_on_every_screens_route(app_db):
    """Рішення власника 15.09.26: скринька невідомих екранів — адмінський
    екран. Роутів шість, і перевіряються ВСІ: закрити сторінку й лишити
    відкритою дошку або картинку означає не закрити нічого — адресу видно в
    історії браузера, а дошка віддає той самий вміст.
    """
    app, session_factory = app_db
    with session_factory() as db:
        puzzle_id = _note(db, name="Піч 3")

    client = _login_as(app, OPERATOR)
    for path in (
        "/screens",
        "/screens/board",
        f"/screens/{puzzle_id}/frame.png",
        f"/screens/{puzzle_id}/zone.png",
    ):
        status, _, _ = client.get(path)
        assert status == 403, f"{path} віддав {status}"

    status, _, _ = client.post(f"/screens/{puzzle_id}/label", {"label": "JOBS"})
    assert status == 403
    status, _, _ = client.post(f"/screens/{puzzle_id}/dismiss", {"dismissed": "on"})
    assert status == 403


def test_operator_cannot_change_anything_through_the_closed_routes(app_db):
    """Відмова мусить бути ДО дії, а не після неї: 403 при вже збереженому
    підписі був би гіршим за відкритий екран."""
    app, session_factory = app_db
    with session_factory() as db:
        puzzle_id = _note(db, name="Піч 3")

    client = _login_as(app, OPERATOR)
    client.post(f"/screens/{puzzle_id}/label", {"label": "JOBS"})
    client.post(f"/screens/{puzzle_id}/dismiss", {"dismissed": "on"})

    with session_factory() as db:
        puzzle = db.get(ScreenPuzzle, puzzle_id)
        assert puzzle.label in (None, ""), "підпис не мав зберегтись"
        assert not puzzle.dismissed, "«неважливо» не мало проставитись"


def test_admin_still_sees_the_board(app_db):
    app, session_factory = app_db
    with session_factory() as db:
        _note(db, name="Піч 3")

    status, _, html = _login_as(app, USER).get("/screens")
    assert status == 200
    assert "Піч 3" in html


def test_rail_and_palette_hide_the_screen_from_an_operator(app_db):
    """Закритий роут без прибраного пункту меню — це кнопка, що віддає
    помилку. Рейку й палітру Ctrl+K тримаємо в тому самому стані, що й гейт."""
    app, _ = app_db

    operator = _login_as(app, OPERATOR)
    _, _, queue_html = operator.get("/")
    assert "Невідомі екрани" not in queue_html

    status, _, palette = operator.get("/palette/commands")
    assert status == 200
    assert "Невідомі екрани" not in palette

    admin = _login_as(app, USER)
    _, _, admin_html = admin.get("/")
    assert "Невідомі екрани" in admin_html
