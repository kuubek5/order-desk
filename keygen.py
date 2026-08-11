"""Інтерактивний генератор ключів активації Order Desk (для власника продукту).

Зручна обгортка над scripts/license_keygen.py: питає дані в діалозі, видає
ключ, зберігає копію в keys/ і кладе ключ у буфер обміну Windows. Запуск:

    python keygen.py            # діалог
    keygen.bat                  # подвійний клік (Windows)

Цей файл, як і приватний ключ, НЕ входить у білд для клієнта
(OrderDesk.spec бере лише app/templates та app/static). Приватний ключ
license_private_key.pem має існувати поруч — це єдине джерело підпису.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.license import encode_license_key

PRIVATE_KEY_PATH = ROOT / "license_private_key.pem"
KEYS_DIR = ROOT / "keys"


def _load_private_key() -> Ed25519PrivateKey:
    if not PRIVATE_KEY_PATH.exists():
        raise SystemExit(
            f"Приватний ключ не знайдено: {PRIVATE_KEY_PATH}\n"
            "Спершу створіть пару: python scripts/license_keygen.py generate-keypair\n"
            "(і збережіть license_private_key.pem у надійному місці — без нього "
            "нові ключі не видати)."
        )
    key = serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{PRIVATE_KEY_PATH} не містить Ed25519-приватний ключ")
    return key


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort copy via Windows `clip`; silently no-op elsewhere."""
    try:
        proc = subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True)
        return proc.returncode == 0
    except Exception:
        return False


def _ask(prompt: str, *, required: bool = True) -> str:
    while True:
        value = input(prompt).strip()
        if value or not required:
            return value
        print("  ! Поле обов'язкове, спробуйте ще раз.")


def _safe_filename(name: str) -> str:
    keep = "-_. "
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in name).strip()
    return cleaned or "client"


def main() -> None:
    print("=" * 58)
    print("  Order Desk — генератор ключів активації")
    print("=" * 58)
    print("Клієнт відкриває екран /license у програмі й діктує вам")
    print("свій machine ID. Введіть його нижче.\n")

    private_key = _load_private_key()

    machine_id = _ask("Machine ID клієнта: ")
    customer = _ask("Назва клієнта / лабораторії: ")
    expires = _ask(
        "Термін дії (РРРР-ММ-ДД) або Enter — без обмеження: ", required=False
    )

    expires_at_iso = None
    if expires:
        try:
            parsed = datetime.fromisoformat(expires)
        except ValueError:
            raise SystemExit(f"Невірна дата: {expires!r}. Формат: 2027-12-31")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        expires_at_iso = parsed.isoformat()

    payload = {
        "machine_id": machine_id,
        "customer": customer,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at_iso,
    }
    key = encode_license_key(payload, private_key)

    # Save a copy so the owner has a record of what was issued to whom.
    KEYS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = KEYS_DIR / f"{_safe_filename(customer)}_{stamp}.txt"
    out_path.write_text(
        f"Клієнт: {customer}\n"
        f"Machine ID: {machine_id}\n"
        f"Термін дії: {expires or 'без обмеження'}\n"
        f"Видано: {payload['issued_at']}\n\n"
        f"{key}\n",
        encoding="utf-8",
    )

    copied = _copy_to_clipboard(key)

    print("\n" + "-" * 58)
    print("КЛЮЧ АКТИВАЦІЇ (клієнт вставляє на екрані /license):\n")
    print(key)
    print("-" * 58)
    print(f"Копію збережено: {out_path}")
    if copied:
        print("Ключ скопійовано в буфер обміну — можна одразу вставити клієнту.")
    print()


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        print(f"\n{exc}")
        sys.exit(1)
