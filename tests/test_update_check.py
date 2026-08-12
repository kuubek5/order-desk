"""app/update_check.py: semver comparison, fetch_latest_release, and
download_and_verify — all with requests.get mocked (see
tests/test_settings_routes.py's MailBox patching for the same "no real
network in tests" convention). launch_silent_install is only exercised in
its dev (non-frozen) no-op branch; the real Windows subprocess/PowerShell
path needs a packaged build and a live installer to test meaningfully."""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.update_check import (
    ReleaseInfo,
    UpdateVerificationError,
    download_and_verify,
    fetch_latest_release,
    is_newer_version,
    launch_silent_install,
)


# --- is_newer_version --------------------------------------------------


@pytest.mark.parametrize(
    "candidate, current, expected",
    [
        ("0.2.0", "0.1.0", True),
        ("1.0.0", "0.9.9", True),
        ("0.1.1", "0.1.0", True),
        ("0.1.0", "0.1.0", False),
        ("0.0.9", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
    ],
)
def test_is_newer_version_ordering(candidate, current, expected):
    assert is_newer_version(candidate, current) is expected


@pytest.mark.parametrize(
    "candidate, current",
    [
        ("not-a-version", "0.1.0"),
        ("1.2", "0.1.0"),
        ("1.2.3.4", "0.1.0"),
        ("1.2.x", "0.1.0"),
        ("", "0.1.0"),
        ("0.2.0", "not-a-version"),
    ],
)
def test_is_newer_version_rejects_unparseable_tags(candidate, current):
    assert is_newer_version(candidate, current) is False


# --- fetch_latest_release -----------------------------------------------


def _release_payload(tag_name: str, *, with_checksum: bool = True) -> dict:
    assets = [{"name": "OrderDesk-Setup-9.9.9.exe", "browser_download_url": "https://example/installer.exe"}]
    if with_checksum:
        assets.append(
            {"name": "OrderDesk-Setup-9.9.9.exe.sha256", "browser_download_url": "https://example/installer.sha256"}
        )
    return {
        "tag_name": tag_name,
        "html_url": "https://github.com/kuubek5/order-desk/releases/tag/" + tag_name,
        "assets": assets,
        "body": "Release notes",
    }


def test_fetch_latest_release_returns_release_when_newer():
    response = MagicMock()
    response.json.return_value = _release_payload("v9.9.9")
    response.raise_for_status.return_value = None
    with patch("app.update_check.requests.get", return_value=response) as mock_get:
        result = fetch_latest_release()
    mock_get.assert_called_once()
    assert result == ReleaseInfo(
        version="9.9.9",
        html_url="https://github.com/kuubek5/order-desk/releases/tag/v9.9.9",
        installer_url="https://example/installer.exe",
        checksum_url="https://example/installer.sha256",
        notes="Release notes",
    )


def test_fetch_latest_release_returns_none_when_not_newer():
    response = MagicMock()
    response.json.return_value = _release_payload("v0.0.1")
    response.raise_for_status.return_value = None
    with patch("app.update_check.requests.get", return_value=response):
        assert fetch_latest_release() is None


def test_fetch_latest_release_returns_none_on_network_error():
    with patch("app.update_check.requests.get", side_effect=OSError("offline")):
        assert fetch_latest_release() is None


def test_fetch_latest_release_returns_none_on_bad_json():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.side_effect = ValueError("not json")
    with patch("app.update_check.requests.get", return_value=response):
        assert fetch_latest_release() is None


def test_fetch_latest_release_returns_none_without_exe_asset():
    payload = _release_payload("v9.9.9", with_checksum=False)
    payload["assets"] = []
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    with patch("app.update_check.requests.get", return_value=response):
        assert fetch_latest_release() is None


def test_fetch_latest_release_never_raises_on_http_error():
    response = MagicMock()
    response.raise_for_status.side_effect = Exception("HTTP 500")
    with patch("app.update_check.requests.get", return_value=response):
        assert fetch_latest_release() is None


# --- download_and_verify --------------------------------------------------


def _release(checksum_url="https://example/installer.sha256"):
    return ReleaseInfo(
        version="9.9.9",
        html_url="https://example/release",
        installer_url="https://example/installer.exe",
        checksum_url=checksum_url,
        notes="",
    )


def _installer_response(content: bytes):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.iter_content.return_value = [content]
    return response


def _checksum_response(hex_digest: str):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.text = hex_digest
    return response


def test_download_and_verify_succeeds_with_matching_checksum(tmp_path):
    content = b"fake installer bytes"
    expected_hash = hashlib.sha256(content).hexdigest()
    installer_response = _installer_response(content)
    checksum_response = _checksum_response(expected_hash)

    with patch("app.update_check.requests.get", side_effect=[installer_response, checksum_response]):
        result_path = download_and_verify(_release(), dest_dir=tmp_path)

    assert result_path.exists()
    assert result_path.read_bytes() == content
    assert result_path.parent == tmp_path


def test_download_and_verify_raises_and_deletes_file_on_mismatch(tmp_path):
    content = b"fake installer bytes"
    wrong_hash = "0" * 64
    installer_response = _installer_response(content)
    checksum_response = _checksum_response(wrong_hash)

    with patch("app.update_check.requests.get", side_effect=[installer_response, checksum_response]):
        with pytest.raises(UpdateVerificationError):
            download_and_verify(_release(), dest_dir=tmp_path)

    leftover = list(Path(tmp_path).glob("*.exe"))
    assert leftover == []


def test_download_and_verify_raises_without_checksum_url(tmp_path):
    content = b"fake installer bytes"
    installer_response = _installer_response(content)

    with patch("app.update_check.requests.get", return_value=installer_response):
        with pytest.raises(UpdateVerificationError):
            download_and_verify(_release(checksum_url=None), dest_dir=tmp_path)

    leftover = list(Path(tmp_path).glob("*.exe"))
    assert leftover == []


# --- launch_silent_install -------------------------------------------------


def test_launch_silent_install_noop_in_dev(tmp_path):
    """Not a frozen/packaged build (the normal state under pytest) — must
    not attempt to spawn any subprocess."""
    with patch("app.update_check.subprocess.Popen") as mock_popen:
        launch_silent_install(tmp_path / "OrderDesk-Setup-9.9.9.exe")
    mock_popen.assert_not_called()


def test_launch_silent_install_frozen_spawns_single_watchdog(tmp_path):
    """Frozen build: exactly one watchdog process is spawned (the watchdog owns
    the install — the app no longer spawns the installer itself, which is what
    raced app shutdown and left the overlay stuck). The Popen must:
      * use CREATE_NO_WINDOW, never DETACHED_PROCESS — powershell is a console
        app and dies silently with no console at all (DETACHED), which is what
        actually left the watchdog log empty; CREATE_NO_WINDOW gives it a hidden
        console so it runs headless.
      * pass DEVNULL std handles (a windowed build has none to inherit).
      * hand the installer's full path to the script.
    """
    import subprocess

    installer = tmp_path / "OrderDesk-Setup-9.9.9.exe"
    with patch("app.update_check.is_frozen", return_value=True), patch(
        "app.update_check.data_dir", return_value=tmp_path
    ), patch("app.update_check.subprocess.Popen") as mock_popen:
        launch_silent_install(installer)

    assert mock_popen.call_count == 1
    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[0] == "powershell"
    assert str(installer) in cmd
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    # The console-mode flag is the crux of the fix. CREATE_NO_WINDOW is set;
    # DETACHED_PROCESS (no console → powershell can't start) must NOT be.
    flags = kwargs["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not (flags & subprocess.DETACHED_PROCESS)
    assert (tmp_path / "update-watchdog.ps1").is_file()
