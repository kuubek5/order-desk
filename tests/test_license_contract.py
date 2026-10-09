"""Контрактний тест: формат ліцензійного ключа узгоджений із kuub.win License Service.

Вектори (tests/fixtures/license_contract.json) підписані ПУБЛІЧНИМ тестовим ключем,
згенеровані сервісом (personal-site/api/tools/make_contract_vectors.py). Якщо цей
тест падає — змінився формат/верифікація в app/license.py, і сервіс видаватиме
невалідні ключі. Див. LICENSE_CONTRACT.md.
"""

import json
from pathlib import Path

import pytest

import app.license as license_module
from app.license import verify_license_key

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "license_contract.json").read_text(encoding="utf-8")
)
CASES = VECTORS["cases"]


@pytest.fixture(autouse=True)
def test_public_key(monkeypatch):
    monkeypatch.setattr(
        license_module, "_PUBLIC_KEY_BYTES", bytes.fromhex(VECTORS["test_public_hex"])
    )


def _tamper(key: str) -> str:
    """Міняє один символ у payload-частині (підпис лишається старим)."""
    payload, sig = key.split(".")
    i = len(payload) // 2
    swapped = "A" if payload[i] != "A" else "B"
    return payload[:i] + swapped + payload[i + 1 :] + "." + sig


def test_vectors_present():
    assert len(CASES) >= 1


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["customer"])
def test_vector_accepted_on_its_machine(case):
    status = verify_license_key(case["key"], case["machine_id"])
    assert status.valid is True, status.reason
    assert status.customer == case["customer"]
    if case["expires_at"] is None:
        assert status.expires_at is None
    else:
        assert status.expires_at is not None
        assert status.expires_at.isoformat() == case["expires_at"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["customer"])
def test_vector_rejected_on_other_machine(case):
    assert verify_license_key(case["key"], case["other_machine_id"]).valid is False


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["customer"])
def test_tampered_key_rejected(case):
    assert verify_license_key(_tamper(case["key"]), case["machine_id"]).valid is False
