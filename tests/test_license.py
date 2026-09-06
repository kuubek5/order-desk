import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import PlainTextResponse

import app.license as license_module
import app.web as web
from app.routers import auth as auth_router_mod
from app.db import Base
from app.license import LicenseStatus, encode_license_key, get_license_status, verify_license_key
from app.models import AppSetting
from app.settings_store import get_setting, set_setting, setting_unreadable

MACHINE_ID = "abc123def456"


@pytest.fixture
def keypair(monkeypatch):
    """A throwaway Ed25519 pair, substituted for the real product public key.

    Tests must never rely on production's private key (it never enters the
    repo) — this issues license keys to itself with its own pair instead.
    """
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(license_module, "_PUBLIC_KEY_BYTES", public_bytes)
    return private_key


def _issue(private_key, machine_id=MACHINE_ID, customer="Test Lab", expires_at=None):
    payload = {
        "machine_id": machine_id,
        "customer": customer,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
    }
    return encode_license_key(payload, private_key)


def test_valid_key_for_correct_machine(keypair):
    key = _issue(keypair)
    status = verify_license_key(key, MACHINE_ID)
    assert status.valid is True
    assert status.customer == "Test Lab"
    assert status.reason is None


def test_key_for_other_machine_is_invalid(keypair):
    key = _issue(keypair, machine_id="some-other-machine")
    status = verify_license_key(key, MACHINE_ID)
    assert status.valid is False
    assert status.reason == "Ключ видано для іншого комп'ютера"


def test_expired_key_is_invalid(keypair):
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    key = _issue(keypair, expires_at=expired)
    status = verify_license_key(key, MACHINE_ID)
    assert status.valid is False
    assert status.reason == "Термін дії ключа сплив"


def test_future_expiry_is_valid(keypair):
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    key = _issue(keypair, expires_at=future)
    status = verify_license_key(key, MACHINE_ID)
    assert status.valid is True


def test_tampered_signature_is_invalid(keypair):
    key = _issue(keypair)
    payload_b64, _, signature_b64 = key.partition(".")
    # Flip a full byte (not just the trailing base64 character, whose last
    # few bits can be padding that decoders ignore and thus not change the
    # decoded bytes at all) so the signature is guaranteed to differ.
    signature = bytearray(license_module._b64url_decode(signature_b64))
    signature[0] ^= 0xFF
    tampered_signature_b64 = license_module._b64url_encode(bytes(signature))
    tampered = f"{payload_b64}.{tampered_signature_b64}"
    status = verify_license_key(tampered, MACHINE_ID)
    assert status.valid is False
    assert status.reason == "Невірний підпис ключа"


@pytest.mark.parametrize(
    "garbage",
    ["", "not-a-key-at-all", "..", "abc.def.ghi", "onlyonepart", "  ", "a" * 5000],
)
def test_garbage_input_never_raises(keypair, garbage):
    status = verify_license_key(garbage, MACHINE_ID)
    assert isinstance(status, LicenseStatus)
    assert status.valid is False
    assert status.reason


def test_get_license_status_reads_stored_key(keypair):
    # get_license_status always checks against the *real* device machine_id
    # (app/license.py's own get_machine_id, not a monkeypatched one), so the
    # issued key must target that real id rather than the fixed MACHINE_ID
    # constant used by the direct verify_license_key tests above.
    real_machine_id = license_module.get_machine_id()
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert get_license_status(db).valid is False

        key = _issue(keypair, machine_id=real_machine_id)
        set_setting(db, "license_key", key)
        db.commit()

        status = get_license_status(db)
        assert status.valid is True
        assert status.customer == "Test Lab"


# --- Encryption-key drift (master.key changed) --------------------------


def _store_under_foreign_key(db, key: str, value: str) -> None:
    """Insert a setting encrypted with a DIFFERENT Fernet key than the app's.

    Simulates a database carried over to a machine whose master.key differs
    (folder migration / fresh install): the ciphertext is intact but the
    current key can no longer decrypt it.
    """
    token = Fernet(Fernet.generate_key()).encrypt(value.encode()).decode()
    db.add(AppSetting(key=key, value_encrypted=token))
    db.commit()


def test_get_setting_returns_none_on_key_mismatch():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _store_under_foreign_key(db, "imap_password", "hunter2")
        # Must degrade to None, not raise InvalidToken.
        assert get_setting(db, "imap_password") is None


def test_setting_unreadable_distinguishes_missing_from_corrupt():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert setting_unreadable(db, "license_key") is False  # nothing stored
        set_setting(db, "imap_password", "ok-with-current-key")
        db.commit()
        assert setting_unreadable(db, "imap_password") is False  # readable
        _store_under_foreign_key(db, "license_key", "some-old-key")
        assert setting_unreadable(db, "license_key") is True  # present, unreadable


def test_get_license_status_flags_key_error_not_missing():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _store_under_foreign_key(db, "license_key", "unreadable-old-key")
        status = get_license_status(db)
        assert status.valid is False
        assert status.key_error is True
        assert status.reason and status.reason != license_module.REASON_NOT_ACTIVATED


def test_gate_redirects_not_500_on_key_error(monkeypatch):
    """The whole point: a key mismatch must reach /license, never crash."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _store_under_foreign_key(db, "license_key", "unreadable-old-key")
    monkeypatch.setattr(web, "SessionLocal", lambda: Session(engine))

    response = asyncio.run(web.license_gate(_request("/"), _call_next_marker))
    assert response.status_code == 303
    assert response.headers["location"] == "/license"


# --- Gate behaviour -----------------------------------------------------


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


async def _call_next_marker(_request):
    return PlainTextResponse("ok")


def test_gate_redirects_unlicensed_root_to_license(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "SessionLocal", lambda: Session(engine))

    response = asyncio.run(web.license_gate(_request("/"), _call_next_marker))
    assert response.status_code == 303
    assert response.headers["location"] == "/license"


def test_gate_does_not_redirect_license_route_itself(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "SessionLocal", lambda: Session(engine))

    response = asyncio.run(web.license_gate(_request("/license"), _call_next_marker))
    assert response.status_code == 200


def test_gate_allows_static_and_health_without_license(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "SessionLocal", lambda: Session(engine))

    for path in ("/static/css/base.css", "/health"):
        response = asyncio.run(web.license_gate(_request(path), _call_next_marker))
        assert response.status_code == 200


def test_gate_redirects_setup_and_login_too(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "SessionLocal", lambda: Session(engine))

    for path in ("/setup", "/login"):
        response = asyncio.run(web.license_gate(_request(path), _call_next_marker))
        assert response.status_code == 303
        assert response.headers["location"] == "/license"


def test_full_activation_cycle_unblocks_the_gate(keypair, monkeypatch):
    # license_submit resolves get_machine_id() (web.py's imported name) and
    # get_license_status resolves app/license.py's own get_machine_id() —
    # both are the *same* real function here (nothing monkeypatched), so
    # issuing for the real device id keeps both call sites in agreement.
    real_machine_id = license_module.get_machine_id()
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(web, "SessionLocal", lambda: Session(engine))

    # Before activation: blocked.
    blocked = asyncio.run(web.license_gate(_request("/"), _call_next_marker))
    assert blocked.status_code == 303

    key = _issue(keypair, machine_id=real_machine_id)
    with Session(engine, expire_on_commit=False) as db:
        response = asyncio.run(
            auth_router_mod.license_submit(
                request=None, license_key=key, db=db
            )
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"  # no admin yet in this fresh DB

    # After activation: no longer redirected to /license.
    unblocked = asyncio.run(web.license_gate(_request("/"), _call_next_marker))
    assert unblocked.status_code == 200


# --- F4 (аудит 06.09.26): /license сам по собі не мав входу ------------------
#
# `license_gate` (app/web.py) завжди пропускає шлях "/license" повз себе —
# інакше нікуди дійти без ключа. Але коли ліцензія вже валідна і в базі є
# оператори, ця сторінка лишалась ЄДИНОЮ діркою: будь-хто без сесії міг
# ввести чужий ключ і повністю переактивувати ліцензію. Анонімний доступ
# лишається лише поки триває активація (ключ невалідний) або на першому
# запуску (операторів нема).


def _fake_request(user_id=None):
    """Мінімальний starlette-сумісний Request із session/headers — той самий
    прийом, що й get_current_user/login_redirect використовують у тестах
    інших роутів (SimpleNamespace замість справжнього ASGI-скоупу)."""
    from types import SimpleNamespace
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


def test_license_form_redirects_to_login_when_valid_and_licensed(keypair):
    """Ліцензія активна, оператор уже є — анонімний GET /license йде на вхід."""
    from app.models import User
    real_machine_id = license_module.get_machine_id()
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(User(username="op", password_hash="x", full_name="Op", role="адмін"))
        key = _issue(keypair, machine_id=real_machine_id)
        set_setting(db, "license_key", key)
        db.commit()

        response = auth_router_mod.license_form(request=_fake_request(None), db=db)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_license_form_open_when_not_yet_activated(keypair):
    """Ключ ще невалідний (активація триває) — сторінка лишається доступна
    без входу, інакше неможливо ввести перший ключ."""
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        response = auth_router_mod.license_form(request=_fake_request(None), db=db)
        assert response.status_code == 200


def test_license_form_open_on_first_run_even_if_licensed(keypair):
    """Ліцензія вже валідна, але операторів у базі ще нема (перший запуск) —
    анонімний доступ лишається, бо саме звідси й починається /setup."""
    real_machine_id = license_module.get_machine_id()
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        key = _issue(keypair, machine_id=real_machine_id)
        set_setting(db, "license_key", key)
        db.commit()

        response = auth_router_mod.license_form(request=_fake_request(None), db=db)
        assert response.status_code == 200


def test_license_form_open_for_a_logged_in_user(keypair):
    """Валідна ліцензія, оператори є, АЛЕ запит з дійсною сесією — не
    редіректити залогіненого адміна геть зі сторінки паспорта ліцензії."""
    from app.models import User
    real_machine_id = license_module.get_machine_id()
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        admin = User(username="op", password_hash="x", full_name="Op", role="адмін", is_active=True)
        db.add(admin)
        key = _issue(keypair, machine_id=real_machine_id)
        set_setting(db, "license_key", key)
        db.commit()

        response = auth_router_mod.license_form(request=_fake_request(admin.id), db=db)
        assert response.status_code == 200


def test_license_submit_requires_login_when_valid_and_licensed(keypair):
    """POST /license з чужим ключем без сесії — редірект на вхід, ключ НЕ
    переактивовується (жоден запис не змінюється)."""
    from app.models import User
    real_machine_id = license_module.get_machine_id()
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(User(username="op", password_hash="x", full_name="Op", role="адмін"))
        original_key = _issue(keypair, machine_id=real_machine_id)
        set_setting(db, "license_key", original_key)
        db.commit()

        other_key = _issue(keypair, machine_id="someone-elses-machine")
        response = asyncio.run(
            auth_router_mod.license_submit(
                request=_fake_request(None), license_key=other_key, db=db,
            )
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert get_setting(db, "license_key") == original_key  # untouched
