"""Offline activation-key licensing: no server, no internet at check time.

A key is `base64url(payload_json) + "." + base64url(ed25519_signature)`,
signed by the product owner's private key (never in this repo — see
`scripts/license_keygen.py`) and verified here with the public key baked
into `_PUBLIC_KEY_BYTES`. The payload binds a key to one machine via
`get_machine_id()`, so a key issued for one PC does not activate another.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
import platform

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

# Public half of the Order Desk product-owner keypair. Generated once,
# offline, with `python scripts/license_keygen.py generate-keypair`; the
# matching private key never enters this repository. Tests must never rely
# on this being the "real" key — they monkeypatch it with a throwaway
# keypair so they can issue license keys to themselves.
_PUBLIC_KEY_BYTES = bytes.fromhex(
    "ced791b7a60fac8ddb4cd57580af17652f786cbe6076b14d856fa7bbe59b3bd1"
)


@dataclass
class LicenseStatus:
    valid: bool
    reason: str | None = None
    customer: str | None = None
    expires_at: datetime | None = None


def _raw_machine_identifier() -> str:
    """Best-effort stable per-install identifier, before hashing."""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                if value:
                    return str(value)
        except OSError as exc:
            logger.warning(
                "Не вдалось прочитати MachineGuid з реєстру, використовую резервний варіант: %s",
                exc,
            )
    else:
        logger.warning(
            "Не Windows-система: machine_id обчислюється з platform.node() — "
            "прийнятно лише для розробки/тестів, не для продакшн-ліцензування"
        )
    return f"fallback:{platform.node()}"


def get_machine_id() -> str:
    """Stable per-PC fingerprint, hashed so the raw Windows GUID is never shown."""
    raw = _raw_machine_identifier()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def encode_license_key(payload: dict, private_key: Ed25519PrivateKey) -> str:
    """Build a signed key string from a payload dict. Used only by the offline issuer."""
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    signature = private_key.sign(payload_b64.encode("ascii"))
    return f"{payload_b64}.{_b64url_encode(signature)}"


def verify_license_key(key: str, machine_id: str) -> LicenseStatus:
    """Parse, verify signature, and check machine/expiry — never raises."""
    key = (key or "").strip()
    if not key or key.count(".") != 1:
        return LicenseStatus(valid=False, reason="Невірний формат ліцензійного ключа")

    payload_b64, _, signature_b64 = key.partition(".")
    if not payload_b64 or not signature_b64:
        return LicenseStatus(valid=False, reason="Невірний формат ліцензійного ключа")

    try:
        signature = _b64url_decode(signature_b64)
    except Exception:
        return LicenseStatus(valid=False, reason="Невірний формат ліцензійного ключа")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(_PUBLIC_KEY_BYTES)
        public_key.verify(signature, payload_b64.encode("ascii"))
    except InvalidSignature:
        return LicenseStatus(valid=False, reason="Невірний підпис ключа")
    except Exception:
        return LicenseStatus(valid=False, reason="Невірний формат ліцензійного ключа")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except Exception:
        return LicenseStatus(valid=False, reason="Невірний формат ліцензійного ключа")

    key_machine_id = payload.get("machine_id")
    customer = payload.get("customer")
    if not isinstance(key_machine_id, str) or not key_machine_id:
        return LicenseStatus(valid=False, reason="Невірний формат ліцензійного ключа")
    if key_machine_id != machine_id:
        return LicenseStatus(
            valid=False, reason="Ключ видано для іншого комп'ютера", customer=customer
        )

    expires_at: datetime | None = None
    expires_at_raw = payload.get("expires_at")
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(str(expires_at_raw))
        except ValueError:
            return LicenseStatus(valid=False, reason="Невірний формат ліцензійного ключа")
        now = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
        if expires_at < now:
            return LicenseStatus(
                valid=False,
                reason="Термін дії ключа сплив",
                customer=customer,
                expires_at=expires_at,
            )

    return LicenseStatus(valid=True, reason=None, customer=customer, expires_at=expires_at)


def get_license_status(db) -> LicenseStatus:
    """Read the stored key (if any) and verify it for this machine."""
    from app.settings_store import get_license_key  # local import avoids a module cycle

    key = get_license_key(db)
    if not key:
        return LicenseStatus(valid=False, reason="Ліцензійний ключ не активовано")
    return verify_license_key(key, get_machine_id())
