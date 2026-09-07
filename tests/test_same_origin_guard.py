"""Origin-стіна для мутацій (ревʼю 07.09.26, core/security HIGH-2).

`SameSite=Strict` відсікає крос-сайтові POST-и, але «site» ігнорує порт:
dev-сервер на 8002 і прод на 8000 — один site, і кука оператора летить у
POST з будь-якої локальної сторінки. Тому мутація мусить приходити з НАШОГО
origin (host:port). Запити без Origin/Sec-Fetch-Site (curl, тести) проходять.
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
from tests.asgi_client import MiniClient


@pytest.fixture(autouse=True)
def _empty_db(monkeypatch):
    """Порожня база в памʼяті на кожен тест цього файлу.

    Без неї тести проходили лише там, де поруч валяється справжній
    kuubmill.db: на чистій машині (тобто в CI) `/login` падав із
    `no such table: app_settings`. А порожньої бази замало: без ліцензії
    застосунок відповідає редиректом на /license РАНІШЕ за origin-стіну, і
    перевірка міряла б не те, що заявлено.
    """
    import app.models  # noqa: F401 — реєструє таблиці в Base.metadata
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
        db.commit()


def _post(headers: dict | None = None):
    client = MiniClient(web.app)
    status, resp_headers, _ = client.post(
        "/login", {"username": "nobody", "password": "x"}, headers=headers
    )
    return status, dict(resp_headers)


def test_foreign_origin_is_refused_even_on_the_same_host():
    status, _ = _post({"origin": "http://127.0.0.1:8002"})
    assert status == 403


def test_null_origin_is_refused():
    status, _ = _post({"origin": "null"})
    assert status == 403


def test_same_site_other_port_without_origin_is_refused():
    status, _ = _post({"sec-fetch-site": "same-site"})
    assert status == 403


def test_own_origin_passes():
    status, _ = _post({"origin": "http://127.0.0.1:8000"})
    assert status != 403


def test_no_origin_header_passes():
    status, _ = _post()
    assert status != 403


def test_get_is_never_blocked_and_frames_stay_same_origin():
    client = MiniClient(web.app)
    status, headers, _ = client.get("/login", headers={"origin": "http://evil.example"})
    assert status != 403
    assert dict(headers).get("x-frame-options") == "SAMEORIGIN"
