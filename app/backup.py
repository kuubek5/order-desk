"""Encrypted full-database backup and restore.

CLAUDE.md section 14 names this an outstanding need: "backup для перенесення
ПК" — moving Order Desk to a new Windows PC or user account. That's exactly
the case the app's normal secret storage can't handle on its own: every
secret in `app_settings` is Fernet-encrypted under a key derived from this
machine's DPAPI store (app/windows_dpapi.py), which by design cannot be
reproduced anywhere else. A raw copy of order_desk.db would carry orders,
clients, and history just fine, but Google Sheet ID / service-account JSON /
IMAP password would come out as undecryptable ciphertext on the new machine.

The backup file sidesteps that by re-encrypting everything — operational
tables and secrets alike — under a password the admin chooses at export
time, independent of DPAPI. On restore, secrets are decrypted with that
password and immediately re-encrypted under whatever DPAPI key the target
machine already has, so the app's normal DPAPI-only-at-rest model picks
back up transparently; nothing outside this module ever has to know a
backup password existed.
"""

import base64
import json
import os
from datetime import date, datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.crypto import decrypt_value, encrypt_value
from app.models import (
    AppSetting,
    Attachment,
    Client,
    ClientNameAlias,
    Comment,
    EmailMessage,
    Order,
    ReworkRecord,
    ShiftNote,
    ShiftNoteImage,
    StatusEvent,
    SyncLog,
    User,
)

FORMAT_VERSION = 1
KDF_ITERATIONS = 480_000

# Parent-first: safe insert order under foreign-key constraints. Restore
# deletes in the reverse of this list (children before parents) and
# inserts in this order (parents before children).
_TABLE_MODELS = [
    User,
    Order,
    EmailMessage,
    StatusEvent,
    Comment,
    ReworkRecord,
    Attachment,
    ClientNameAlias,
    Client,
    SyncLog,
    ShiftNote,
    ShiftNoteImage,
]


class BackupPasswordError(Exception):
    """Wrong backup password — Fernet's own auth tag failed to verify."""


class BackupFormatError(Exception):
    """Not a recognizable Order Desk backup file."""


def _row_to_dict(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in sa_inspect(obj).mapper.column_attrs:
        val = getattr(obj, col.key)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        out[col.key] = val
    return out


def _dict_to_row(model: type, data: dict[str, Any]):
    kwargs: dict[str, Any] = {}
    for col in sa_inspect(model).columns:
        name = col.name
        if name not in data:
            continue
        val = data[name]
        if val is not None and isinstance(val, str):
            py_type = col.type.python_type if hasattr(col.type, "python_type") else None
            if py_type is datetime:
                val = datetime.fromisoformat(val)
            elif py_type is date:
                val = date.fromisoformat(val)
        kwargs[name] = val
    return model(**kwargs)


def _derive_key(password: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def create_backup(session: Session, password: str) -> bytes:
    """Serialize every table plus decrypted settings, encrypt under `password`.

    Returns the full backup file's bytes (a small JSON envelope; the actual
    data sits inside `envelope["payload"]`, a Fernet token).
    """
    tables: dict[str, list[dict[str, Any]]] = {}
    for model in _TABLE_MODELS:
        rows = session.query(model).all()
        tables[model.__tablename__] = [_row_to_dict(r) for r in rows]

    settings: dict[str, str] = {}
    for row in session.query(AppSetting).all():
        if row.value_encrypted is None:
            continue
        settings[row.key] = decrypt_value(row.value_encrypted)

    payload = json.dumps({"tables": tables, "settings": settings}, ensure_ascii=False).encode("utf-8")

    salt = os.urandom(16)
    key = _derive_key(password, salt)
    token = Fernet(key).encrypt(payload)

    envelope = {
        "format_version": FORMAT_VERSION,
        "app": "order-desk",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "kdf": "pbkdf2-sha256",
        "kdf_iterations": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "payload": token.decode("ascii"),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def restore_backup(session: Session, file_bytes: bytes, password: str) -> dict[str, int]:
    """Replace all operational data and secrets with what's in the backup.

    Destructive by design — this is a restore, not a merge. Raises
    `BackupFormatError` for anything that isn't an Order Desk backup file
    and `BackupPasswordError` for a password that doesn't match (Fernet's
    own authentication tag fails to verify rather than silently producing
    garbage, so a wrong password is always caught here, never left to
    surface as corrupted data downstream).
    """
    try:
        envelope = json.loads(file_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupFormatError("Файл резервної копії пошкоджений або це не бекап Order Desk.") from exc

    if not isinstance(envelope, dict) or envelope.get("app") != "order-desk" or "payload" not in envelope or "salt" not in envelope:
        raise BackupFormatError("Файл резервної копії пошкоджений або це не бекап Order Desk.")

    iterations = envelope.get("kdf_iterations", KDF_ITERATIONS)
    try:
        salt = base64.b64decode(envelope["salt"])
    except (ValueError, TypeError) as exc:
        raise BackupFormatError("Файл резервної копії пошкоджений або це не бекап Order Desk.") from exc

    key = _derive_key(password, salt, iterations)

    try:
        payload = Fernet(key).decrypt(envelope["payload"].encode("ascii"))
    except InvalidToken as exc:
        raise BackupPasswordError("Невірний пароль резервної копії.") from exc

    data = json.loads(payload.decode("utf-8"))
    tables: dict[str, list[dict[str, Any]]] = data.get("tables", {})
    settings: dict[str, str] = data.get("settings", {})

    counts: dict[str, int] = {}

    for model in reversed(_TABLE_MODELS):
        session.query(model).delete()

    for model in _TABLE_MODELS:
        rows = tables.get(model.__tablename__, [])
        for row_data in rows:
            session.add(_dict_to_row(model, row_data))
        counts[model.__tablename__] = len(rows)

    session.query(AppSetting).delete()
    for key_name, value in settings.items():
        session.add(AppSetting(key=key_name, value_encrypted=encrypt_value(value)))
    counts["app_settings"] = len(settings)

    session.commit()
    return counts
