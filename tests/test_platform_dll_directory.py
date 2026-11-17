"""Чужі програми запускаються без нашої теки DLL (17.11.26).

Завантажувач PyInstaller ставить `SetDllDirectory(<тека програми>\\_internal)`, і
Windows передає це кожному дочірньому процесу. Sum3D, відкритий з KuubMill,
тримав наші DLL — інсталятор оновлення бачив «Lab  Application» і двічі
відкотився. Тут перевіряємо, що в момент запуску тека скинута, а після —
повернута (наш процес мусить і далі знаходити свої DLL).
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import pytest

from app import platform_windows

pytestmark = pytest.mark.skipif(os.name != "nt", reason="SetDllDirectory є лише у Windows")


def _dll_directory() -> str:
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetDllDirectoryW(len(buf), buf)
    return buf.value


@pytest.fixture
def frozen_like_dll_dir(tmp_path):
    kernel32 = ctypes.windll.kernel32
    before = _dll_directory() or None
    kernel32.SetDllDirectoryW(str(tmp_path))
    yield str(tmp_path)
    kernel32.SetDllDirectoryW(before)


def test_sum3d_is_started_without_our_dll_directory(frozen_like_dll_dir, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda target: seen.append(_dll_directory()), raising=False)
    platform_windows.open_with_default_app(Path("C:/x/2026-11-17_22-53-40.cam"))
    assert seen == [""], "у момент запуску Sum3D тека DLL KuubMill мусить бути скинута"
    assert _dll_directory() == frozen_like_dll_dir, "після запуску — повернута"


def test_dll_directory_is_restored_even_when_launch_fails(frozen_like_dll_dir, monkeypatch):
    def boom(_target):
        raise OSError("немає асоціації")

    monkeypatch.setattr(os, "startfile", boom, raising=False)
    with pytest.raises(OSError):
        platform_windows.open_with_default_app(Path("C:/x/a.cam"))
    assert _dll_directory() == frozen_like_dll_dir
