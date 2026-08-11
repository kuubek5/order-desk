"""Offline license key generator/issuer for the Order Desk product owner.

This script is NOT packaged into the Windows build (see `OrderDesk.spec` —
`scripts/` is absent from `datas`) and must never ship to a customer machine.
It is the only place the private signing key is allowed to exist.

Usage:
    python scripts/license_keygen.py generate-keypair
    python scripts/license_keygen.py issue --machine-id <id> --customer "Назва клієнта" [--expires 2027-12-31]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.license import encode_license_key

PRIVATE_KEY_PATH = Path(__file__).resolve().parents[1] / "license_private_key.pem"


def generate_keypair() -> None:
    if PRIVATE_KEY_PATH.exists():
        raise SystemExit(
            f"{PRIVATE_KEY_PATH} вже існує. Видаліть його свідомо, якщо справді "
            "потрібна нова пара ключів (усі раніше видані ключі перестануть працювати)."
        )
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    PRIVATE_KEY_PATH.write_bytes(pem)

    raw_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print(f"Приватний ключ збережено у: {PRIVATE_KEY_PATH}")
    print("НЕ комітьте цей файл у git — .gitignore вже його виключає.")
    print()
    print("Публічний ключ (hex) — вставити в app/license.py, _PUBLIC_KEY_BYTES:")
    print(raw_public.hex())


def _load_private_key() -> Ed25519PrivateKey:
    if not PRIVATE_KEY_PATH.exists():
        raise SystemExit(
            f"{PRIVATE_KEY_PATH} не знайдено. Спершу виконайте: "
            "python scripts/license_keygen.py generate-keypair"
        )
    key = serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{PRIVATE_KEY_PATH} не містить Ed25519-приватний ключ")
    return key


def issue(machine_id: str, customer: str, expires: str | None) -> None:
    private_key = _load_private_key()

    expires_at_iso = None
    if expires:
        # Accept a plain date (YYYY-MM-DD); store end-of-day UTC so "expires
        # 2027-12-31" covers the whole day rather than expiring at midnight.
        parsed = datetime.fromisoformat(expires)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        expires_at_iso = parsed.isoformat()

    payload = {
        "machine_id": machine_id.strip(),
        "customer": customer.strip(),
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at_iso,
    }
    key = encode_license_key(payload, private_key)
    print("Ліцензійний ключ (передати клієнту для вставки на екрані /license):")
    print(key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("generate-keypair", help="Створити нову пару ключів Ed25519")

    issue_parser = subparsers.add_parser("issue", help="Видати ліцензійний ключ клієнту")
    issue_parser.add_argument("--machine-id", required=True, help="Fingerprint машини клієнта")
    issue_parser.add_argument("--customer", required=True, help="Назва клієнта/лабораторії")
    issue_parser.add_argument("--expires", default=None, help="Термін дії, напр. 2027-12-31")

    args = parser.parse_args()
    if args.command == "generate-keypair":
        generate_keypair()
    elif args.command == "issue":
        issue(args.machine_id, args.customer, args.expires)


if __name__ == "__main__":
    main()
