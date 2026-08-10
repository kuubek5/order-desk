import os

import pytest
from cryptography.fernet import Fernet

from app.windows_dpapi import DPAPIError, load_or_create_master_key, protect_data, unprotect_data


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI test")


def test_dpapi_round_trip_is_not_plaintext():
    plain = b"order-desk-secret"
    protected = protect_data(plain)

    assert protected != plain
    assert unprotect_data(protected) == plain


def test_master_key_is_persisted_and_is_a_valid_fernet_key(tmp_path):
    first = load_or_create_master_key(tmp_path)
    second = load_or_create_master_key(tmp_path)

    assert first == second
    assert (tmp_path / "master.key").read_bytes() != first.encode()
    Fernet(first.encode())


def test_corrupt_existing_master_key_fails_closed_without_replacing_it(tmp_path):
    key_path = tmp_path / "master.key"
    corrupt = b"not a DPAPI blob"
    key_path.write_bytes(corrupt)

    with pytest.raises(DPAPIError, match="CryptUnprotectData failed"):
        load_or_create_master_key(tmp_path)

    assert key_path.read_bytes() == corrupt
