"""Ворота релізу однією командою (`scripts/release_gates.ps1`).

Скрипт мусить повторювати CI (`.github/workflows/tests.yml`), а не жити своїм життям:
розійдуться — локально зелене знову нічого не обіцятиме. І він нічого не випускає сам.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_gates.ps1"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_script_has_bom_for_windows_powershell_5():
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_gates_repeat_the_ci_commands():
    ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    text = _text()
    assert "python -m ruff check ." in ci and "'-m', 'ruff', 'check'" in text
    assert "python -m mypy app --ignore-missing-imports" in ci
    assert "'-m', 'mypy', 'app', '--ignore-missing-imports'" in text
    assert "pytest -q -n auto --dist loadfile" in ci
    assert "'-n', 'auto', '--dist', 'loadfile'" in text
    assert ".mypy-baseline" in ci and ".mypy-baseline" in text


def test_gates_check_the_things_that_already_broke_a_release():
    text = _text()
    # Лінтер лише по файлах репозиторію (чернетки поза git у CI не існують).
    assert "'ls-files', '*.py'" in text
    # Після пушу — ОБИДВА прогони і файли в релізному репозиторії, не в репозиторії коду.
    assert "@('Tests', 'Release')" in text
    assert "kuubek5/order-desk-releases" in text
    # Файл поза git не бачить ні лінтер, ні тег (17.11.26: так новий модуль пройшов би повз ruff).
    assert "'--untracked-files=all'" in text
    # Пропущений повний прогін не може виглядати як готовність до тега.
    assert "if ($Quick) { Finish 2 }" in text


def test_script_never_commits_tags_or_pushes():
    text = _text()
    for forbidden in ("'commit'", "'push'", "'-a'", "'add'", "'reset'", "'checkout'", "release', 'create", "'upload'"):
        assert forbidden not in text, forbidden


def _run(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args],
        capture_output=True,
        timeout=120,
        check=False,
    )


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="скрипт розрахований на Windows PowerShell",
)
def test_changelog_gate_on_real_files(tmp_path):
    log = tmp_path / "CHANGELOG.md"
    head = "# Журнал\n\n## [Незалежно від версії]\n\n"

    log.write_text(head + "## [1.2.3] — 2026-11-17\n### Додано\n- Перший пункт.\n- Другий.\n\n## [1.2.2]\n- Старе.\n", encoding="utf-8")
    assert _run("-Mode", "changelog", "-ChangelogPath", str(log), "-Version", "1.2.3").returncode == 0

    # Розділ є, але порожній — саме так виходить «мовчазний» реліз.
    log.write_text(head + "## [1.2.3] — 2026-11-17\n### Додано\n\n## [1.2.2]\n- Старе.\n", encoding="utf-8")
    assert _run("-Mode", "changelog", "-ChangelogPath", str(log), "-Version", "1.2.3").returncode == 1

    # Розділу немає зовсім: версію підняли, а журнал не проштампували.
    assert _run("-Mode", "changelog", "-ChangelogPath", str(log), "-Version", "9.9.9").returncode == 1
