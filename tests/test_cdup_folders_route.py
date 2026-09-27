"""Лінивий роут списку тек для автодоповнення на екрані дублікатів.

757 <option> більше не рендеряться в тілі сторінки (1.7-3.5с на проді);
поле ручного вводу підвантажує їх окремим запитом. Головне — НЕ зламати
ручне об'єднання тек: сторінка мусить лишитись робочою, а роут — віддавати
теки з кешу, коли вони там є.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests.asgi_client import MiniClient

ADMIN = ("cdupadmin", "Cd@p-Admin-1")


@pytest.fixture
def app_db(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import app.web as web
    from app import license as license_module
    from app.auth import hash_password
    from app.db import Base
    from app.models import User
    from app.routers import deps
    from app.services import duplicates_cache

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
        license_module, "_PUBLIC_KEY_BYTES",
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    from app.settings_store import set_setting

    duplicates_cache.reset_for_tests()
    with session_factory() as db:
        set_setting(db, "license_key", license_module.encode_license_key(
            {"machine_id": license_module.get_machine_id(), "customer": "t"}, private,
        ))
        db.add(User(
            username=ADMIN[0], password_hash=hash_password(ADMIN[1]),
            full_name=ADMIN[0], role="адмін",
        ))
        db.commit()
    yield web.app
    duplicates_cache.reset_for_tests()


def _login(app):
    client = MiniClient(app)
    status, _, _ = client.login(*ADMIN)
    assert status in (200, 302, 303), status
    return client


def test_page_has_empty_datalist_and_working_manual_form(app_db):
    """Сторінка: datalist порожній (не 757 рядків), але форма ручного
    об'єднання ціла — це головне, що не можна зламати."""
    client = _login(app_db)
    status, _, html = client.get("/clients/duplicates")
    assert status == 200
    assert '<datalist id="cdup-folders"></datalist>' in html
    assert 'name="name_a" list="cdup-folders"' in html
    assert 'action="/clients/duplicates/folder-merge"' in html


def test_lazy_route_serves_folder_options_from_cache(app_db):
    """Коли в кеші є теки — лінивий роут віддає їх як <option>."""
    from app.services import duplicates_cache

    snap = duplicates_cache.Snapshot(folder_names=["Іван Ніколаєв", "LekaLab"])
    import app.services.duplicates_cache as dc
    with dc._lock:
        dc._snapshot = snap

    client = _login(app_db)
    status, _, html = client.get("/clients/duplicates/folders")
    assert status == 200
    assert '<option value="LekaLab">' in html
    assert '<option value="Іван Ніколаєв">' in html


def test_lazy_route_empty_when_cache_cold(app_db):
    """Кеш ще порожній — роут віддає порожньо (200), не падає й не сканує диск."""
    client = _login(app_db)
    status, _, html = client.get("/clients/duplicates/folders")
    assert status == 200
    assert "<option" not in html
