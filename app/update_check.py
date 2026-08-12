"""Check GitHub Releases for a newer Order Desk build, verify, and install it.

Background-loop shape copied verbatim from app/web.py's mail/sheet sync
workers (see _mail_sync_tick/_mail_sync_worker there): a `_tick()` doing one
attempt (importable and testable without threads), a `_worker(stop_event)`
looping `stop_event.wait(...)` around it, and a module-level "last known
result" so request handlers never have to hit the network to answer "is
there an update available right now".

This module must never let a network failure escape to its caller — the lab
PC may simply be offline, and a background update check crashing the whole
worker thread (or, worse, the request that triggered a manual check) is far
worse than silently finding nothing this time.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Event

import requests

from app.__version__ import VERSION
from app.runtime import data_dir, is_frozen

logger = logging.getLogger(__name__)

GITHUB_REPO = "kuubek5/order-desk"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
REQUEST_TIMEOUT_SECONDS = 5
DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

UPDATE_CHECK_INITIAL_DELAY_SECONDS = 30
UPDATE_CHECK_INTERVAL_SECONDS = 86400  # once a day


class UpdateVerificationError(Exception):
    """Raised when a downloaded installer's checksum cannot be confirmed.

    Never install a file this exception was raised for — the caller must
    treat this as fatal, not a warning.
    """


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    html_url: str
    installer_url: str
    checksum_url: str | None
    notes: str


def _parse_semver(version: str) -> tuple[int, int, int] | None:
    """Best-effort `MAJOR.MINOR.PATCH` parse. Returns None for anything that
    doesn't look like a clean three-part integer version (extra pre-release
    suffixes, malformed tags, etc.) so callers can treat it as "unknown"
    rather than crash on a weird GitHub tag."""
    parts = version.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def is_newer_version(candidate: str, current: str) -> bool:
    """True only if `candidate` parses as a valid semver strictly greater
    than `current`. Any unparseable version (either side) is treated as
    "not newer" — silently ignoring a weird tag is safer than accidentally
    offering a downgrade or crashing the update check."""
    candidate_tuple = _parse_semver(candidate)
    current_tuple = _parse_semver(current)
    if candidate_tuple is None or current_tuple is None:
        return False
    return candidate_tuple > current_tuple


def _find_asset_url(assets: list[dict], *, suffixes: tuple[str, ...]) -> str | None:
    for asset in assets:
        name = str(asset.get("name") or "")
        if name.lower().endswith(suffixes):
            return asset.get("browser_download_url")
    return None


def fetch_latest_release() -> ReleaseInfo | None:
    """GET the latest GitHub release and return it only if it is a real,
    parseable, strictly newer version than this build. Returns None on:
    no newer release, any network/HTTP/JSON error, or a release with no
    usable installer asset. Never raises — this is called from a background
    thread with no one watching for exceptions, and from request handlers
    that must stay responsive even when GitHub or the network is down.
    """
    try:
        response = requests.get(RELEASES_API_URL, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - deliberately catch-all, see docstring
        logger.warning("Update check failed: %s", exc)
        return None

    tag_name = str(payload.get("tag_name") or "").strip()
    version = tag_name[1:] if tag_name.startswith("v") else tag_name
    if not is_newer_version(version, VERSION):
        return None

    assets = payload.get("assets") or []
    installer_url = _find_asset_url(assets, suffixes=(".exe",))
    if not installer_url:
        logger.warning("Latest GitHub release %s has no .exe installer asset", tag_name)
        return None
    checksum_url = _find_asset_url(assets, suffixes=(".sha256", ".sha256.txt"))

    return ReleaseInfo(
        version=version,
        html_url=str(payload.get("html_url") or ""),
        installer_url=installer_url,
        checksum_url=checksum_url,
        notes=str(payload.get("body") or ""),
    )


# Last background-check result, read by web.py/templates without ever
# touching the network on a request. Same rationale as app/web.py's
# _sync_heartbeats: one module-level slot, written only by the worker
# thread, read freely by request-handling threads — a single dict/variable
# assignment is atomic under the GIL, so no lock is needed.
_latest_known_release: ReleaseInfo | None = None


def get_known_update() -> ReleaseInfo | None:
    return _latest_known_release


def _update_check_tick() -> None:
    global _latest_known_release
    _latest_known_release = fetch_latest_release()


def _update_check_worker(stop_event: Event) -> None:
    if stop_event.wait(UPDATE_CHECK_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            _update_check_tick()
        except Exception:
            logger.exception("Unexpected background update check failure")
        stop_event.wait(UPDATE_CHECK_INTERVAL_SECONDS)


def _updates_dir() -> Path:
    directory = data_dir() / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_and_verify(release: ReleaseInfo, dest_dir: Path | None = None) -> Path:
    """Download `release.installer_url` into `dest_dir` (default: the
    per-machine `updates` folder under the app data directory) and verify
    its SHA-256 against `release.checksum_url`.

    A missing checksum asset is treated exactly like a mismatched one:
    raises UpdateVerificationError and never returns an unverified file.
    This is a deliberate security requirement, not a convenience default.
    """
    directory = dest_dir if dest_dir is not None else _updates_dir()
    directory.mkdir(parents=True, exist_ok=True)
    installer_path = directory / f"OrderDesk-Setup-{release.version}.exe"

    response = requests.get(release.installer_url, stream=True, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()
    with installer_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
            if chunk:
                handle.write(chunk)

    if not release.checksum_url:
        installer_path.unlink(missing_ok=True)
        raise UpdateVerificationError(
            "Реліз не має файлу контрольної суми — встановлення скасовано з міркувань безпеки"
        )

    checksum_response = requests.get(release.checksum_url, timeout=REQUEST_TIMEOUT_SECONDS)
    checksum_response.raise_for_status()
    expected = checksum_response.text.strip().split()[0].lower()
    actual = _sha256_of_file(installer_path)

    if actual != expected:
        installer_path.unlink(missing_ok=True)
        raise UpdateVerificationError(
            "Контрольна сума завантаженого файлу не збігається — встановлення скасовано"
        )

    _strip_mark_of_the_web(installer_path)
    return installer_path


def _strip_mark_of_the_web(path: Path) -> None:
    """Remove the Zone.Identifier NTFS alternate data stream (Mark of the Web)
    from a file whose SHA-256 we have already verified above.

    A file carrying MOTW is routed through Windows SmartScreen when launched,
    which can suspend an unsigned installer behind a prompt no one is there to
    click during a silent auto-update. Stripping it is safe here precisely
    because trust is already established cryptographically by the checksum
    check — this is not a security downgrade, it is removing a redundant gate
    on a file we have independently proven authentic. Best-effort and
    Windows-only: any failure (no such stream, non-NTFS, non-Windows) is
    ignored, since the stream simply may not exist."""
    try:
        os.remove(f"{path}:Zone.Identifier")
    except OSError:
        pass


_WATCHDOG_SCRIPT = r"""
$logDir = Join-Path $env:LOCALAPPDATA 'OrderDesk\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'update-watchdog.log'
function W($m) { "$(Get-Date -Format o) $m" | Out-File -FilePath $log -Append -Encoding utf8 }
$exe = $args[0]
$installer = $args[1]
$installerStem = [System.IO.Path]::GetFileNameWithoutExtension($installer)
$installerLog = Join-Path $logDir 'update-installer.log'
W "watchdog start; exe=$exe installer=$installer"
# The watchdog OWNS the install: it launches the installer itself (rather than
# the dying app spawning it) so nothing races app shutdown. The installer's
# PrepareToInstall step stops the running app via `--shutdown`; we just wait for
# the whole installer to finish, then relaunch.
#
# -NoNewWindow is load-bearing, not cosmetic: it forces Start-Process to use
# CreateProcess (UseShellExecute=false) instead of ShellExecute. ShellExecute
# routes an unsigned, unknown-reputation exe through Windows SmartScreen, which
# SUSPENDS the installer behind an invisible "Windows protected your PC" prompt
# — hanging the whole update with an empty installer log and no files replaced.
# CreateProcess bypasses that App-Reputation gate. (The installer is unsigned
# and new every release, so it never earns reputation.)
try {
    Start-Process -FilePath $installer -NoNewWindow `
        -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$installerLog"
    W "installer launched"
} catch {
    W "installer launch FAILED: $_"
}
# Inno forks a child *.tmp installer and the parent setup.exe exits early, so
# wait until no installer process (by stem) remains before relaunching, up to
# 3 minutes.
$deadline = (Get-Date).AddMinutes(3)
Start-Sleep -Seconds 2
while ((Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like ($installerStem + '*') }).Count -gt 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
}
W "installer finished; relaunching with health retries"
Start-Sleep -Seconds 2
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Name OrderDesk -ErrorAction SilentlyContinue)) {
        W "launch attempt $i"
        Start-Process -FilePath $exe -ArgumentList '--open-browser'
    }
    Start-Sleep -Seconds 4
    $c = (& curl.exe -s -o NUL -w '%{http_code}' --max-time 3 --noproxy '*' http://127.0.0.1:8000/health 2>$null)
    if ($c -eq '200') { W 'health 200, done'; break }
}
W 'watchdog end'
"""


def launch_silent_install(installer_path: Path) -> None:
    """Spawn a single detached watchdog that installs the verified update and
    relaunches the freshly installed app.

    Relaunch can't ride on the installer's [Run] step: under /VERYSILENT that
    entry is `skipifsilent` (an interactive-only postinstall), and a headless
    session has no desktop to launch it from anyway. So a watchdog handles it.

    The watchdog — not the app — launches the installer. This inverts the older
    design where the app spawned the installer *and* the watchdog separately and
    then let the installer shut it down: that raced app death against the second
    (watchdog) spawn, and when the app won the race the watchdog was never
    created — leaving the update overlay stuck forever with no watchdog log.
    Making the long-lived watchdog the installer's parent removes that race
    entirely and the installer replaces the (now cleanly stopped) app's locked
    files from a stable process rather than from the app that is dying under it.

    Three hard-won details:
      * CREATE_NO_WINDOW, NOT DETACHED_PROCESS — this is THE reason a
        spawned watchdog produced an empty log even after the DEVNULL fix
        below. DETACHED_PROCESS gives the child *no console at all*, and
        powershell.exe is a console-subsystem app: with no console it fails to
        initialize its host and dies silently before running a single line.
        CREATE_NO_WINDOW instead gives it a hidden console, so it runs headless
        (no visible window) exactly as intended. Verified with a direct probe:
        DETACHED never wrote its marker file, CREATE_NO_WINDOW always did. The
        watchdog still outlives the app — Windows child processes are not killed
        when the parent exits (no kill-on-close job object is in play here).
      * `stdin/stdout/stderr=DEVNULL` — the packaged build is windowed, so it
        has no standard handles to inherit; DEVNULL hands the child real ones.
        Necessary but, on its own, not sufficient (see the console point above).
      * the script is a `.ps1` FILE run with `-File` (not an inline `-Command`
        string) — an inline one-liner proved fragile to parse/escape; a file on
        disk parses cleanly.

    The watchdog logs every step to update-watchdog.log and passes `/LOG` to the
    installer (update-installer.log) so a future failure is diagnosable.

    No-ops in dev (not a frozen/packaged build).
    """
    if not is_frozen():
        logger.info("launch_silent_install пропущено, не пакований білд — нема інсталятора для запуску")
        return

    exe_path = Path(sys.executable)

    # CREATE_NO_WINDOW (hidden console), not DETACHED_PROCESS (no console) —
    # powershell needs a console to start; see the docstring. CREATE_NEW_PROCESS_
    # GROUP keeps a Ctrl+C to the app's group from reaching the watchdog. Only
    # meaningful on Windows (this whole function no-ops off it, since is_frozen()
    # requires os.name == "nt" to matter — see app/runtime.py::data_dir), but
    # resolved defensively so importing this module never fails on a non-Windows
    # dev machine.
    spawn_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )

    watchdog_path = data_dir() / "update-watchdog.ps1"
    watchdog_path.write_text(_WATCHDOG_SCRIPT, encoding="utf-8")
    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(watchdog_path),
            str(exe_path),
            str(installer_path),
        ],
        creationflags=spawn_flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
