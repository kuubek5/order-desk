"""Кожен розділ реєстру справді гейтиться у своєму роуті.

Навіщо окремий файл із підняттям застосунку. `tests/test_section_gate.py`
перевіряє ДОМЕННУ частину — стан, аудиторію, кого блокувати. Але сам гейт
працює лише тоді, коли роут розділу його КЛИЧЕ, а це окремий рядок в окремому
файлі, який легко забути: до 10.09.26 у реєстрі стояв один розділ, і виклик
теж був один. Додати рядок у `SECTIONS` і не додати виклик — це закритий на
папері розділ, який насправді відкритий усім, і жоден доменний тест такого не
побачить.

Тому тут застосунок піднімається по-справжньому, оператор входить, усі розділи
зачиняються — і кожен шлях із реєстру мусить віддати екран-блокатор. Новий
розділ без виклику гейта завалить саме цей тест.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import web
from app.db import Base
from app.services import section_gate as sg
from tests.asgi_client import MiniClient


OPERATOR = ("gate-op", "Gate-Op-1")
ADMIN = ("gate-adm", "Gate-Adm-1")


@pytest.fixture(autouse=True)
def _app_db(monkeypatch):
    """Порожня база в памʼяті + дійсна ліцензія.

    Без ліцензії застосунок відповідає редиректом на /license РАНІШЕ за гейт
    розділів, і тест міряв би не те, що заявлено (та сама пастка, що в
    tests/test_same_origin_guard.py).
    """
    import app.models  # noqa: F401 — реєструє таблиці в Base.metadata
    from app import license as license_module
    from app.auth import hash_password
    from app.models import User
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
        for (name, password), role in ((OPERATOR, "оператор"), (ADMIN, "адмін")):
            db.add(
                User(
                    username=name,
                    password_hash=hash_password(password),
                    full_name=name,
                    role=role,
                )
            )
        db.commit()
    # Бейджі рейки кешуються на дві секунди — між тестами кеш чистимо, інакше
    # «зачинено» з попереднього тесту протікає в наступний.
    deps.clear_global_badge_cache()
    yield
    deps.clear_global_badge_cache()


def _close_everything():
    from app.routers import deps

    with deps.SessionLocal() as db:
        for key in sg.SECTIONS:
            sg.set_section_state(db, key, "shutter")
        db.commit()


def _open_everything():
    from app.routers import deps

    with deps.SessionLocal() as db:
        for key in sg.SECTIONS:
            sg.set_section_state(db, key, sg.OPEN)
        db.commit()


def _client(user) -> MiniClient:
    client = MiniClient(web.app)
    status, _, _ = client.login(*user)
    assert status in (200, 303), f"вхід не вдався: {status}"
    return client


@pytest.mark.parametrize("section", sorted(sg.SECTIONS))
def test_every_registered_section_is_actually_gated(section):
    """Розділ у реєстрі без виклику гейта = закритий лише на папері."""
    _close_everything()
    client = _client(OPERATOR)
    path = sg.SECTIONS[section]["path"]

    status, _, body = client.get(path)

    assert status == 200, f"{section}: {path} віддав {status}"
    assert "blk-art" in body or "section-blocked" in body or "Зачинено" in body, (
        f"{section}: {path} НЕ показав блокатор — найімовірніше, у роуті немає "
        f"виклику blocked_response(..., \"{section}\")"
    )


@pytest.mark.parametrize("section", sorted(sg.SECTIONS))
def test_an_open_section_is_not_blocked(section):
    """Зворотний бік: відкритий розділ гейт не чіпає."""
    _open_everything()
    client = _client(OPERATOR)

    status, _, body = client.get(sg.SECTIONS[section]["path"])

    assert status == 200
    assert "blk-art" not in body, f"{section}: відкритий розділ показав блокатор"


def test_admin_sees_every_section_even_when_all_are_closed():
    _close_everything()
    client = _client(ADMIN)
    for section, meta in sg.SECTIONS.items():
        status, _, body = client.get(meta["path"])
        assert status == 200, f"{section}: {status}"
        assert "blk-art" not in body, f"{section}: адміна не можна блокувати"


def test_admin_rail_names_the_closed_sections():
    """«Яка сторінка зараз зачинена» має бути видно з будь-якого екрана —
    інакше закритий тиждень тому розділ знаходять за скаргою оператора."""
    from app.routers import deps

    with deps.SessionLocal() as db:
        sg.set_section_state(db, "furnaces", "shutter")
        sg.set_section_state(db, "stats", sg.OPEN)
        db.commit()
    deps.clear_global_badge_cache()

    _, _, body = _client(ADMIN).get("/")

    assert "rail-closed" in body
    assert "Пічки" in body


def test_operator_rail_never_shows_the_closed_notice():
    from app.routers import deps

    with deps.SessionLocal() as db:
        sg.set_section_state(db, "furnaces", "shutter")
        sg.set_section_state(db, "stats", sg.OPEN)
        db.commit()
    deps.clear_global_badge_cache()

    _, _, body = _client(OPERATOR).get("/")

    assert "rail-closed" not in body
