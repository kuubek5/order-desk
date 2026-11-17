"""Пакет налаштувань Sum3D (`tools/sum3d-probe/sum3d_pack.ps1`).

Скрипт збирає стратегії, архів фрез і словники в один zip, щоб розбирати їх на іншому ПК.
Сторожі: бере лише білий список, лише читає, KuubMill його не запускає. Плюс справжній
запуск на підробленій теці Sum3D — зелений текстовий сторож ще не доводить, що zip збирається.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "tools" / "sum3d-probe"
PACK = KIT / "sum3d_pack.ps1"
WRAPPER = KIT / "3_pack_settings.cmd"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_pack_files_exist_and_ship_with_the_app():
    assert PACK.is_file() and WRAPPER.is_file()
    spec = (ROOT / "KuubMill.spec").read_text(encoding="utf-8")
    assert '("tools/sum3d-probe", "tools/sum3d-probe")' in spec
    assert "3_pack_settings.cmd" in (KIT / "README.md").read_text(encoding="utf-8")


def test_pack_script_has_bom_and_wrapper_is_plain_crlf():
    # Без BOM Windows PowerShell 5.1 читає кирилицю як ANSI; BOM у cmd ламає перший рядок.
    assert PACK.read_bytes().startswith(b"\xef\xbb\xbf")
    data = WRAPPER.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.count(b"\r\n") == data.count(b"\n") > 0


def test_pack_script_only_reads_and_stays_off_the_network():
    text = _text(PACK).lower()
    for forbidden in (
        "remove-item",
        "move-item",
        "rename-item",
        "set-itemproperty",
        "new-itemproperty",
        "reg add",
        "reg delete",
        "reg import",
        "reg export",
        "invoke-webrequest",
        "invoke-restmethod",
        "net.webclient",
        "start-bitstransfer",
        "start-process",
        "stop-process",
        ".kill()",
        "dllimport",
        "camwinc.exe",
    ):
        assert forbidden not in text, forbidden
    # Файли Sum3D відкриваються лише на читання й без блокування.
    assert "[io.fileaccess]::read, $share" in text
    assert "[io.fileshare]::readwrite -bor [io.fileshare]::delete" in text


def test_wrapper_runs_the_pack_with_default_settings():
    text = WRAPPER.read_text(encoding="utf-8")
    assert '-File "%~dp0sum3d_pack.ps1"' in text
    assert "-SumDir" not in text and "-MaxTotalMB" not in text


def test_app_never_runs_the_pack():
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "sum3d_pack" not in text and "3_pack_settings" not in text, path.name


def _fake_sum3d(root: Path) -> None:
    machine = root / "Dental" / "CAM-system" / "ST" / "CORiTEC 250i PRO+_Tolik"
    machine.mkdir(parents=True)
    (machine / "Zirkon V14.cfg").write_bytes(b"cfg")
    (machine / "Zirkon V14.txt").write_bytes(b"txt")
    (machine / "Цирконій тест.cfg").write_bytes(b"cfg2")
    (machine / "patient.stl").write_bytes(b"stl")
    (machine / "GPSettings.ini").write_bytes(b"ini")
    (root / "millbox" / "Strategy").mkdir(parents=True)
    (root / "millbox" / "Strategy" / "strategies_vocabulary.csv").write_bytes(b"csv")
    (root / "Jobs").mkdir()
    (root / "Jobs" / "job.cam").write_bytes(b"cam")
    (root / "Jobs" / "notes.txt").write_bytes(b"notes")
    for name in ("Tools.arl", "Tools.arl.250908122720", "Machines.arl", "params_codes.txt"):
        (root / name).write_bytes(b"R4SUM_TOOL 1 4")
    for name in ("licence.lic", "dongle.key", "camwinc.exe", "GPSettings.ini", "readme.txt"):
        (root / name).write_bytes(b"no")


def _run(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PACK), *args],
        capture_output=True,
        timeout=120,
        check=False,
    )


needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell.exe") is None,
    reason="скрипт розрахований на Windows PowerShell",
)


@needs_powershell
def test_real_run_packs_only_the_white_list(tmp_path):
    sum_dir = tmp_path / "iCAM"
    sum_dir.mkdir()
    _fake_sum3d(sum_dir)
    before = sorted((p.relative_to(sum_dir).as_posix(), p.read_bytes()) for p in sum_dir.rglob("*") if p.is_file())
    out = tmp_path / "out"

    done = _run("-SumDir", str(sum_dir), "-OutRoot", str(out))

    assert done.returncode == 0, done.stdout[-600:]
    (archive,) = out.rglob("*.zip")
    with zipfile.ZipFile(archive) as zf:
        names = {name.split("/", 1)[1] for name in zf.namelist() if "/" in name}
        manifest = zf.read("manifest.txt").decode("utf-8-sig")
        assert zf.read(next(n for n in zf.namelist() if n.endswith("/Tools.arl"))) == b"R4SUM_TOOL 1 4"
    assert names == {
        "Tools.arl",
        "Tools.arl.250908122720",
        "Machines.arl",
        "params_codes.txt",
        "millbox/Strategy/strategies_vocabulary.csv",
        "Dental/CAM-system/ST/CORiTEC 250i PRO+_Tolik/Zirkon V14.cfg",
        "Dental/CAM-system/ST/CORiTEC 250i PRO+_Tolik/Zirkon V14.txt",
        "Dental/CAM-system/ST/CORiTEC 250i PRO+_Tolik/Цирконій тест.cfg",
    }
    assert "Zirkon V14.cfg" in manifest and "patient.stl" not in manifest
    # Тека Sum3D лишилась байт у байт такою самою.
    after = sorted((p.relative_to(sum_dir).as_posix(), p.read_bytes()) for p in sum_dir.rglob("*") if p.is_file())
    assert after == before


@needs_powershell
def test_dry_run_lists_without_a_zip_and_missing_folder_is_an_error(tmp_path):
    sum_dir = tmp_path / "iCAM"
    sum_dir.mkdir()
    _fake_sum3d(sum_dir)
    out = tmp_path / "out"

    done = _run("-SumDir", str(sum_dir), "-OutRoot", str(out), "-DryRun")

    assert done.returncode == 0
    assert not list(out.rglob("*.zip"))
    (manifest,) = out.rglob("manifest.txt")
    assert "Tools.arl.250908122720" in manifest.read_text(encoding="utf-8-sig")

    missing = _run("-SumDir", str(tmp_path / "nowhere"), "-OutRoot", str(tmp_path / "out2"))
    assert missing.returncode == 1
    assert not list((tmp_path / "out2").rglob("*.zip"))


@needs_powershell
def test_oversized_file_is_skipped_and_named(tmp_path):
    sum_dir = tmp_path / "iCAM"
    sum_dir.mkdir()
    (sum_dir / "Tools.arl").write_bytes(b"x" * (2 * 1024 * 1024))
    (sum_dir / "Machines.arl").write_bytes(b"small")
    out = tmp_path / "out"

    done = _run("-SumDir", str(sum_dir), "-OutRoot", str(out), "-MaxFileMB", "1")

    assert done.returncode == 0
    (archive,) = out.rglob("*.zip")
    with zipfile.ZipFile(archive) as zf:
        assert [n.split("/", 1)[1] for n in zf.namelist() if "/" in n] == ["Machines.arl"]
        assert "Tools.arl : завеликий" in zf.read("manifest.txt").decode("utf-8-sig")
