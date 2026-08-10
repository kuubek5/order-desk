"""Windows DPAPI-backed storage for the packaged application's master key.

The blob is protected for the Windows account that created it.  It is not a
portable backup: recovery still requires a separately escrowed key strategy.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import secrets


CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DPAPIError(RuntimeError):
    """DPAPI could not protect or recover the packaged application's key."""


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _require_windows() -> None:
    if os.name != "nt":
        raise DPAPIError("Windows DPAPI is only available on Windows")


def _input_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    # create_string_buffer keeps the memory alive for the duration of the API
    # call.  Its trailing NUL is intentionally excluded from cbData.
    buffer = ctypes.create_string_buffer(data)
    blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def protect_data(data: bytes) -> bytes:
    """Protect bytes for the current Windows user without displaying UI."""
    _require_windows()
    if not data:
        raise DPAPIError("refusing to protect an empty master key")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, input_buffer = _input_blob(data)
    output_blob = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Order Desk master key",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    del input_buffer  # explicitly document the buffer lifetime
    if not ok:
        error = ctypes.get_last_error()
        raise DPAPIError(f"CryptProtectData failed (Windows error {error})")

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def unprotect_data(blob: bytes) -> bytes:
    """Recover bytes protected by :func:`protect_data` for this Windows user."""
    _require_windows()
    if not blob:
        raise DPAPIError("master.key is empty")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, input_buffer = _input_blob(blob)
    output_blob = _DATA_BLOB()
    description = wintypes.LPWSTR()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not ok:
        error = ctypes.get_last_error()
        raise DPAPIError(f"CryptUnprotectData failed (Windows error {error})")

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def _validate_fernet_key(key: bytes) -> str:
    try:
        decoded = base64.urlsafe_b64decode(key)
        text = key.decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise DPAPIError("master.key does not contain a valid Fernet key") from exc
    if len(decoded) != 32:
        raise DPAPIError("master.key does not contain a valid Fernet key")
    return text


def load_or_create_master_key(data_dir: Path) -> str:
    """Load ``master.key`` or create it exactly once.

    Existing unreadable or corrupt blobs are never replaced.  An exclusive
    create also prevents two concurrently starting processes from silently
    choosing different keys.
    """
    _require_windows()
    key_path = data_dir / "master.key"

    if key_path.exists():
        try:
            return _validate_fernet_key(unprotect_data(key_path.read_bytes()))
        except OSError as exc:
            raise DPAPIError(f"cannot read existing master.key: {exc}") from exc

    data_dir.mkdir(parents=True, exist_ok=True)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    protected = protect_data(key)
    try:
        with key_path.open("xb") as file:
            file.write(protected)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        # Another process won the first-start race.  Its key is authoritative.
        try:
            return _validate_fernet_key(unprotect_data(key_path.read_bytes()))
        except OSError as exc:
            raise DPAPIError(f"cannot read existing master.key: {exc}") from exc

    return _validate_fernet_key(key)
