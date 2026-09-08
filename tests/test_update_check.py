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


def test_download_and_verify_reports_progress_with_total(tmp_path):
    """Оверлей показує «X з Y МБ» — total береться з Content-Length, done росте
    з кожним шматком. Без цього оператор дивився на нерухомий напис хвилинами."""
    chunks = [b"a" * 10, b"b" * 10, b"c" * 5]
    content = b"".join(chunks)
    installer_response = _installer_response(content)
    installer_response.iter_content.return_value = chunks
    installer_response.headers = {"Content-Length": str(len(content))}
    checksum_response = _checksum_response(hashlib.sha256(content).hexdigest())
    seen = []
    with patch("app.update_check._http_get", side_effect=[installer_response, checksum_response]):
        download_and_verify(_release(), dest_dir=tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert seen == [(10, 25), (20, 25), (25, 25)]


def test_download_and_verify_progress_survives_missing_content_length(tmp_path):
    content = b"fake installer bytes"
    installer_response = _installer_response(content)
    installer_response.headers = {}
    checksum_response = _checksum_response(hashlib.sha256(content).hexdigest())
    seen = []
    with patch("app.update_check._http_get", side_effect=[installer_response, checksum_response]):
        download_and_verify(_release(), dest_dir=tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert seen == [(len(content), 0)]


def test_human_update_error_hides_requests_internals():
    import requests as rq
    from app.update_check import human_update_error

    for exc, expect in (
        (rq.exceptions.ConnectTimeout("HTTPSConnectionPool(...)"), "проксі"),
        (rq.exceptions.ReadTimeout("Read timed out"), "обірвалось"),
        (rq.exceptions.ConnectionError("Max retries exceeded"), "розірвано"),
        (UpdateVerificationError("Контрольна сума не збігається"), "Контрольна сума"),
        (RuntimeError("boom"), "kuubmill.log"),
    ):
        text = human_update_error(exc)
        assert expect in text and "HTTPSConnectionPool" not in text and "Max retries" not in text


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


# ── Службовий реліз агента верстата не має вимикати оновлення ──────────────
# Аудит 08.09.26. Складання агента (.github/workflows/agent-build.yml) публікує
# `KMillAgent-Setup.exe` під тегом `agent-latest` у ТОЙ САМИЙ репозиторій
# релізів. GitHub віддає /releases/latest як найновіший за датою створення, тож
# один такий реліз здатен стати «найновішим» для KuubMill. Далі раніше все
# ламалось мовчки: тег не читався як версія → None → екран малював зелене «у вас
# найновіша версія», а асет обирався як «перший .exe у списку» → міг виявитись
# інсталятором агента. Обидві гілки тепер під сторожем.


def _agent_release_payload() -> dict:
    """Те, що GitHub віддасть, якщо `agent-latest` виявиться найновішим."""
    return {
        "tag_name": "agent-latest",
        "html_url": "https://github.com/kuubek5/order-desk-releases/releases/tag/agent-latest",
        "assets": [
            {
                "name": "KMillAgent-Setup.exe",
                "browser_download_url": "https://example/KMillAgent-Setup.exe",
            }
        ],
        "body": "Інсталятор агента для ПК верстата.",
    }


def test_unparseable_tag_reports_problem_instead_of_up_to_date():
    release, problem = update_check._release_from_payload(_agent_release_payload())
    assert release is None
    assert problem is not None
    assert "agent-latest" in problem


def test_tick_stores_problem_for_unparseable_tag():
    response = MagicMock()
    response.json.return_value = _agent_release_payload()
    response.raise_for_status.return_value = None
    with patch("app.update_check._http_get", return_value=response):
        assert _update_check_tick() is True
    assert update_check.get_known_update() is None
    # Головне: мовчання тут читалося б на екрані як «у вас найновіша версія».
    assert update_check.get_check_problem() is not None


def test_successful_check_clears_previous_problem():
    update_check._last_check_problem = "стара проблема"
    response = MagicMock()
    response.json.return_value = _release_payload("v0.0.1")
    response.raise_for_status.return_value = None
    with patch("app.update_check._http_get", return_value=response):
        assert _update_check_tick() is True
    assert update_check.get_check_problem() is None


def test_installer_asset_chosen_by_name_not_by_extension():
    """Реліз KuubMill, у якому поруч лежить чужий .exe і він ПЕРШИЙ у списку."""
    payload = _release_payload("v9.9.9")
    payload["assets"] = [
        {
            "name": "KMillAgent-Setup.exe",
            "browser_download_url": "https://example/agent.exe",
        }
    ] + payload["assets"]
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    with patch("app.update_check._http_get", return_value=response):
        result = fetch_latest_release()
    assert result is not None
    assert result.installer_url == "https://example/installer.exe"


def test_newer_release_without_kuubmill_installer_reports_problem():
    payload = _release_payload("v9.9.9")
    payload["assets"] = [
        {
            "name": "KMillAgent-Setup.exe",
            "browser_download_url": "https://example/agent.exe",
        }
    ]
    release, problem = update_check._release_from_payload(payload)
    assert release is None
    assert problem is not None


def test_agent_workflow_publishes_prerelease():
    """Корінь проблеми — у файлі складання, не в Python.

    GitHub виключає prerelease з /releases/latest, тож саме цей прапорець не дає
    службовому релізу агента стати «найновішою версією KuubMill». Правка в
    update_check.py — друга лінія оборони, ця — перша.
    """
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "agent-build.yml"
    ).read_text(encoding="utf-8")
    create_line = next(
        (line for line in workflow.splitlines() if "gh release create agent-latest" in line),
        None,
    )
    assert create_line is not None, "зник крок публікації агента — перевір workflow"
    start = workflow.index(create_line)
    assert "--prerelease" in workflow[start : start + 400]


# ── Відкат після невдалого оновлення ───────────────────────────────────────
# Досі, якщо нова версія не піднімалась, сторож просто виходив: лабораторія
# лишалась зі зламаним оновленням до приїзду людини з ноутбуком (аудит
# 08.09.26). Перевірено на робочій машині запуском справжнього скрипта по
# фальшивому порту й фальшивому процесу — прод не чіпали.


def test_watchdog_script_is_written_with_a_bom(tmp_path, monkeypatch):
    """utf-8-SIG, не просто utf-8. Найдорожча знахідка цієї перевірки.

    Застосунок запускає `powershell`, а це Windows PowerShell 5.1. БЕЗ BOM він
    читає файл у системному кодуванні (cp1251), а не в UTF-8: кириличні
    коментарі перетворюються на сміття, яке ламає РОЗБІР скрипта цілком.
    Сторож не запускається, лог порожній, оновлення висить без сліду.

    Доти скрипт випадково жив, бо був суто латинським. Перша ж українська
    літера в коментарі його вбила — спіймано запуском на робочій машині ще до
    того, як це потрапило в цех.
    """
    monkeypatch.setattr(update_check, "is_frozen", lambda: True)
    monkeypatch.setattr(update_check, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(update_check.subprocess, "Popen", lambda *a, **k: None)

    update_check.launch_silent_install(tmp_path / "KuubMill-Setup-9.9.9.exe")

    written = (tmp_path / "update-watchdog.ps1").read_bytes()
    assert written[:3] == b"\xef\xbb\xbf", (
        "скрипт сторожа без BOM — Windows PowerShell 5.1 прочитає кирилицю як "
        "cp1251 і не зможе його розібрати"
    )


def test_watchdog_script_parses_under_windows_powershell():
    """Скрипт мусить розбиратись саме тим інтерпретатором, який його запускає.

    `pwsh` 7 читає UTF-8 за замовчуванням і помилки не бачить — тому перевіряти
    треба `powershell.exe` (5.1), інакше тест дає хибний спокій.
    """
    import shutil as _shutil
    import subprocess
    import tempfile

    if _shutil.which("powershell") is None:
        pytest.skip("Windows PowerShell недоступний")

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "watchdog.ps1"
        script.write_text(update_check._WATCHDOG_SCRIPT, encoding="utf-8-sig")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$e=$null; $t=$null; "
             f"[void][System.Management.Automation.Language.Parser]::ParseFile("
             f"'{script}', [ref]$t, [ref]$e); "
             f"if ($e) {{ $e[0].Message; exit 1 }} else {{ exit 0 }}"],
            capture_output=True, text=True, timeout=60,
        )
    assert result.returncode == 0, f"скрипт не розбирається: {result.stdout}{result.stderr}"


def test_rollback_installer_is_found_for_the_current_version(tmp_path):
    """Відкочуватись треба на версію, яка ЗАРАЗ стоїть — отже потрібен саме її
    інсталятор. Тека `updates` не чиститься, тож він там лежить."""
    (tmp_path / "KuubMill-Setup-0.11.6.exe").write_bytes(b"x")
    (tmp_path / "KuubMill-Setup-0.11.5.exe").write_bytes(b"x")

    found = update_check.find_rollback_installer("0.11.6", updates_dir=tmp_path)
    assert found is not None and found.name == "KuubMill-Setup-0.11.6.exe"


def test_no_rollback_installer_is_not_an_error(tmp_path):
    """Версію, поставлену РУКАМИ, відкотити нічим — інсталятор у теку не
    потрапляв. Це нормальний стан, а не збій: сторож просто голосно запише."""
    assert update_check.find_rollback_installer("0.11.8", updates_dir=tmp_path) is None


def test_the_newest_snapshot_wins(tmp_path):
    from app.pre_update_backup import pre_update_dir

    db = tmp_path / "kuubmill.db"
    folder = pre_update_dir(db)
    folder.mkdir(parents=True, exist_ok=True)
    for stamp in ("20260907-100000", "20260907-213104", "20260906-090000"):
        (folder / f"kuubmill-pre-0.11.6-{stamp}.db").write_bytes(b"x")

    found = update_check.find_pre_update_snapshot("0.11.6", db)
    assert found is not None and "213104" in found.name


def test_watchdog_receives_the_rollback_material(tmp_path, monkeypatch):
    """Чим відкочуватись, рахує ЗАСТОСУНОК, поки живий: після встановлення
    сторож уже не має способу дізнатись, яка версія була до нього."""
    captured = {}

    monkeypatch.setattr(update_check, "is_frozen", lambda: True)
    monkeypatch.setattr(update_check, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(update_check, "find_rollback_installer", lambda v: Path("C:/old.exe"))
    monkeypatch.setattr(update_check, "find_pre_update_snapshot", lambda v, db: Path("C:/snap.db"))
    monkeypatch.setattr(
        update_check.subprocess, "Popen",
        lambda args, **k: captured.setdefault("args", args),
    )

    update_check.launch_silent_install(tmp_path / "KuubMill-Setup-9.9.9.exe")

    args = captured["args"]
    joined = " ".join(str(a) for a in args)
    assert "old.exe" in joined, "інсталятор для відкату не переданий сторожу"
    assert "snap.db" in joined, "знімок бази не переданий сторожу"


def test_rollback_waits_for_the_forked_installer_like_the_install_path_does():
    """Inno породжує дочірній *.tmp, а батьківський setup.exe виходить ОДРАЗУ.

    Прямий шлях встановлення це знає й чекає, поки зникнуть усі процеси з
    іменем інсталятора. У відкаті цього спершу не було: `-Wait` дочекався б
    лише батька, тобто застосунок стартував би посеред заміни власних файлів —
    і відкат, покликаний рятувати, добив би встановлення.

    Тест із фальшивим `.cmd` цього НЕ ловить: `.cmd` виходить синхронно, тож
    різниці між `-Wait` і очікуванням за іменем не видно. Тому сторож
    структурний — звіряє, що обидва шляхи чекають однаково (08.09.26).
    """
    script = update_check._WATCHDOG_SCRIPT
    # Обидва шляхи мусять мати свій цикл очікування за іменем процесу.
    waits = script.count("-like ($")
    assert waits >= 2, (
        "у відкаті немає очікування дочірнього процесу інсталятора — "
        "застосунок стартує посеред заміни власних файлів"
    )
    # Коментарі відкидаємо: у них слово `-Wait` є навмисно, як пояснення чому
    # ми ним НЕ користуємось.
    code = chr(10).join(
        line for line in script.splitlines() if not line.strip().startswith("#")
    )
    launch = code[code.index("$rollbackInstaller -NoNewWindow"):]
    launch = launch[: launch.index("$rollbackStem")]
    assert "-Wait" not in launch, (
        "відкат покладається на -Wait, а він чекає лише батьківський процес"
    )
    assert "rollbackStem" in code
