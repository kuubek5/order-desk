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

import app.update_check as update_check
from app.update_check import (
    ReleaseInfo,
    UpdateVerificationError,
    UPDATE_CHECK_INTERVAL_SECONDS,
    UPDATE_CHECK_RETRY_SECONDS,
    _update_check_tick,
    _update_check_worker,
    download_and_verify,
    fetch_latest_release,
    get_known_update,
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
    assets = [{"name": "KuubMill-Setup-9.9.9.exe", "browser_download_url": "https://example/installer.exe"}]
    if with_checksum:
        assets.append(
            {"name": "KuubMill-Setup-9.9.9.exe.sha256", "browser_download_url": "https://example/installer.sha256"}
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
    with patch("app.update_check._http_get", return_value=response) as mock_get:
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
    with patch("app.update_check._http_get", return_value=response):
        assert fetch_latest_release() is None


def test_fetch_latest_release_returns_none_on_network_error():
    with patch("app.update_check._http_get", side_effect=OSError("offline")):
        assert fetch_latest_release() is None


def test_fetch_latest_release_returns_none_on_bad_json():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.side_effect = ValueError("not json")
    with patch("app.update_check._http_get", return_value=response):
        assert fetch_latest_release() is None


def test_fetch_latest_release_returns_none_without_exe_asset():
    payload = _release_payload("v9.9.9", with_checksum=False)
    payload["assets"] = []
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    with patch("app.update_check._http_get", return_value=response):
        assert fetch_latest_release() is None


def test_fetch_latest_release_never_raises_on_http_error():
    response = MagicMock()
    response.raise_for_status.side_effect = Exception("HTTP 500")
    with patch("app.update_check._http_get", return_value=response):
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

    with patch("app.update_check._http_get", side_effect=[installer_response, checksum_response]):
        result_path = download_and_verify(_release(), dest_dir=tmp_path)

    assert result_path.exists()
    assert result_path.read_bytes() == content
    assert result_path.parent == tmp_path


def test_download_and_verify_raises_and_deletes_file_on_mismatch(tmp_path):
    content = b"fake installer bytes"
    wrong_hash = "0" * 64
    installer_response = _installer_response(content)
    checksum_response = _checksum_response(wrong_hash)

    with patch("app.update_check._http_get", side_effect=[installer_response, checksum_response]):
        with pytest.raises(UpdateVerificationError):
            download_and_verify(_release(), dest_dir=tmp_path)

    leftover = list(Path(tmp_path).glob("*.exe"))
    assert leftover == []


def test_download_and_verify_raises_without_checksum_url(tmp_path):
    content = b"fake installer bytes"
    installer_response = _installer_response(content)

    with patch("app.update_check._http_get", return_value=installer_response):
        with pytest.raises(UpdateVerificationError):
            download_and_verify(_release(checksum_url=None), dest_dir=tmp_path)

    leftover = list(Path(tmp_path).glob("*.exe"))
    assert leftover == []


# --- launch_silent_install -------------------------------------------------


def test_launch_silent_install_noop_in_dev(tmp_path):
    """Not a frozen/packaged build (the normal state under pytest) — must
    not attempt to spawn any subprocess."""
    with patch("app.update_check.subprocess.Popen") as mock_popen:
        launch_silent_install(tmp_path / "KuubMill-Setup-9.9.9.exe")
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

    installer = tmp_path / "KuubMill-Setup-9.9.9.exe"
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


# --- _update_check_tick: transport-failure vs clean-check signal --------


@pytest.fixture(autouse=True)
def _reset_known_release():
    """Each tick test starts and ends with an empty known-update slot so the
    module-level state can't leak between tests (or into the live app)."""
    update_check._latest_known_release = None
    yield
    update_check._latest_known_release = None


def _ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_tick_returns_true_and_stores_release_when_newer():
    response = _ok_response(_release_payload("v9.9.9"))
    with patch("app.update_check._http_get", return_value=response):
        assert _update_check_tick() is True
    assert get_known_update() is not None
    assert get_known_update().version == "9.9.9"


def test_tick_returns_true_but_stores_none_when_up_to_date():
    # Reached GitHub successfully, just nothing newer → True, slot cleared to
    # None. This is the case a plain None return could NOT distinguish from a
    # network failure, which is the whole point of the split.
    response = _ok_response(_release_payload("v0.0.1"))
    with patch("app.update_check._http_get", return_value=response):
        assert _update_check_tick() is True
    assert get_known_update() is None


def test_tick_returns_false_and_preserves_previous_release_on_network_error():
    # First: a good tick finds an update.
    good = _ok_response(_release_payload("v9.9.9"))
    with patch("app.update_check._http_get", return_value=good):
        assert _update_check_tick() is True
    found = get_known_update()
    assert found is not None

    # Then: a transient failure must NOT wipe it — returns False, slot kept.
    with patch("app.update_check._http_get", side_effect=OSError("offline")):
        assert _update_check_tick() is False
    assert get_known_update() is found


# --- worker interval selection: retry soon on failure, daily on success -


class _StopAfter:
    """Fake Event whose wait() returns False the first N times (letting the
    loop run) then True (breaking it), recording every wait() duration so the
    test can assert which interval the worker chose."""

    def __init__(self, allow_iterations: int):
        self._remaining = allow_iterations
        self.waits: list[float] = []
        self._set = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._remaining <= 0:
            # Mirror a real threading.Event: once wait() reports the event is
            # set, is_set() must agree — otherwise the worker's
            # `while not stop_event.is_set()` loop never exits.
            self._set = True
            return True
        self._remaining -= 1
        return False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


def test_worker_sleeps_retry_interval_after_failed_tick():
    stop = _StopAfter(allow_iterations=1)  # initial wait + one loop body
    with patch("app.update_check._update_check_tick", return_value=False):
        _update_check_worker(stop)
    # waits[0] is the initial delay; waits[1] is the post-tick interval.
    assert stop.waits[-1] == UPDATE_CHECK_RETRY_SECONDS


def test_worker_sleeps_daily_interval_after_successful_tick():
    stop = _StopAfter(allow_iterations=1)
    with patch("app.update_check._update_check_tick", return_value=True):
        _update_check_worker(stop)
    assert stop.waits[-1] == UPDATE_CHECK_INTERVAL_SECONDS


def test_worker_treats_tick_exception_as_failure_and_retries_soon():
    stop = _StopAfter(allow_iterations=1)
    with patch("app.update_check._update_check_tick", side_effect=RuntimeError("boom")):
        _update_check_worker(stop)
    assert stop.waits[-1] == UPDATE_CHECK_RETRY_SECONDS


# --- Update feed must be a PUBLIC repo, not the private source repo ----------
#
# The source repo is private; the update check is anonymous (no token). If the
# feed ever points at a private repo, GitHub's API returns 404 for an
# unauthenticated request and auto-update goes silently dark. This guards the
# split: updates are read from the dedicated public releases repo.


def test_update_feed_points_at_public_releases_repo_not_private_source():
    assert update_check.GITHUB_REPO == "kuubek5/order-desk-releases"
    # The source repo name must NOT be the feed — that one is private.
    assert update_check.GITHUB_REPO != "kuubek5/order-desk"
    assert update_check.RELEASES_API_URL == (
        "https://api.github.com/repos/kuubek5/order-desk-releases/releases/latest"
    )


def test_update_check_sends_no_authorization_header():
    """The feed is public on purpose so no token ships in the installed app.
    A stray Authorization header would mean a secret leaked into the client."""
    captured = {}

    def _fake_get(url, **kwargs):
        captured["headers"] = kwargs.get("headers")
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {}
        return resp

    with patch.object(update_check, "_http_get", _fake_get):
        update_check._fetch_release_payload()

    # No auth header passed by our code (session defaults carry none either).
    assert not (captured.get("headers") or {}).get("Authorization")
