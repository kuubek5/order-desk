"""Читання чужого файлу так, щоб не заважати його власникові.

Звичайний `open()` на Windows дозволяє іншій програмі читати й писати файл,
але НЕ видаляти й не перейменовувати його, поки він відкритий у нас. Sum3D і
програма верстата зберігають файли саме так — пишуть тимчасовий і
перейменовують поверх старого. Якщо в цю мить файл відкритий у KuubMill,
збереження в них падає з «файл зайнято». Для розвідки чужих файлів це
неприйнятно: ми лише дивимось.

`open_shared` відкриває файл на читання з дозволом іншим на читання, запис І
видалення (FILE_SHARE_DELETE). Поза Windows — звичайний `open`: там видалення
відкритого файлу й так дозволене. Нічого не пише за конструкцією.
"""

from __future__ import annotations

import os
import sys
from typing import BinaryIO

_GENERIC_READ = 0x80000000
_SHARE_ALL = 0x1 | 0x2 | 0x4          # READ | WRITE | DELETE
_OPEN_EXISTING = 3
_ATTR_NORMAL = 0x80


def open_shared(path: str) -> BinaryIO:
    """Двійковий файл лише на читання, що не заважає власникові його
    переписати чи прибрати. OSError — як у `open`."""
    if sys.platform != "win32":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    handle = kernel32.CreateFileW(str(path), _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, _ATTR_NORMAL, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise
    return os.fdopen(fd, "rb")


def read_shared(path: str, limit: int) -> bytes:
    """Перші `limit` байтів файлу через `open_shared`."""
    with open_shared(path) as handle:
        return handle.read(limit)
