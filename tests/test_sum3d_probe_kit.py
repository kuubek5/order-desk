"""Набір розвідки Sum3D (`tools/sum3d-probe`) і читач його звітів (`kmill_sum3d_probe`).

Сторожі безпеки: набір лише читає, KuubMill його не запускає, а запущений руками він за
замовчуванням не лізе до відкритої Sum3D і не займає останнє місце ліцензії.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services import sum3d_probe_report as spr
from app.services.mcp_tools import TOOLS_BY_NAME

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "tools" / "sum3d-probe"
MAIN = KIT / "sum3d_probe.ps1"
STEP = KIT / "sum3d_probe_step.ps1"

# Єдині функції Sum3dDll_x64.dll, які дозволено кликати: усі лише читають.
ALLOWED_EXPORTS = {
    "?setSumDirectoryInst@@YAXPEBD@Z",
    "?initialize@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@XZ",
    "?getMachineNameList@@YA_NPEADH@Z",
    "?getUsedStrategyName@@YA?AW4ISum3dDLLErrorCode@ISum3dDll@@PEBDPEAD@Z",
    "?getSpaceLeftOnRAW@@YAHPEBD@Z",
    "?getSpaceLeftOnRAW_WithCurves@@YAHPEBDAEAHAEAPEAXAEAPEAN@Z",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_kit_files_exist_and_ship_with_the_app():
    for name in ("sum3d_probe.ps1", "sum3d_probe_step.ps1", "1_dry_run.cmd", "2_probe.cmd", "README.md"):
        assert (KIT / name).is_file(), name
    spec = (ROOT / "KuubMill.spec").read_text(encoding="utf-8")
    assert '("tools/sum3d-probe", "tools/sum3d-probe")' in spec


def test_powershell_files_have_bom_for_windows_powershell_5():
    # Без BOM Windows PowerShell 5.1 читає кирилицю як ANSI і ламає рядки.
    for path in (MAIN, STEP):
        assert path.read_bytes().startswith(b"\xef\xbb\xbf"), path.name


def test_cmd_files_have_no_bom_and_use_crlf():
    # BOM ламає перший рядок cmd; з LF замість CRLF cmd може не знайти мітку goto
    # (закінчення рядків тримає .gitattributes).
    for name in ("1_dry_run.cmd", "2_probe.cmd"):
        data = (KIT / name).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), name
        assert data.count(b"\r\n") == data.count(b"\n") > 0, name
    assert "tools/sum3d-probe/*.cmd text eol=crlf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


def test_step_script_calls_only_read_only_sum3d_functions():
    text = _text(STEP)
    entry_points = set(re.findall(r'EntryPoint\s*=\s*"([^"]+)"', text))
    assert entry_points == ALLOWED_EXPORTS
    # Кожен DllImport бібліотеки Sum3D мусить мати явний EntryPoint зі списку.
    assert len(re.findall(r"\[DllImport\(Dll\b", text)) == len(ALLOWED_EXPORTS)
    # Жодних інших бібліотек Sum3D.
    assert set(re.findall(r'DllImport\("([^"]+)"', text)) == {"kernel32.dll"}
    assert re.findall(r'const string Dll = "([^"]+)"', text) == ["Sum3dDll_x64.dll"]


def test_main_script_checks_the_same_function_list():
    text = _text(MAIN)
    assert set(re.findall(r"'(\?[A-Za-z_]\w*@@[^']+)'", text)) == ALLOWED_EXPORTS


@pytest.mark.parametrize("path", [MAIN, STEP], ids=lambda p: p.name)
def test_scripts_do_not_write_delete_or_talk_to_network(path):
    text = _text(path)
    for forbidden in (
        "Remove-Item",
        "Set-ItemProperty",
        "New-ItemProperty",
        "reg add",
        "reg delete",
        "reg import",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Net.WebClient",
        "Start-BitsTransfer",
        "Stop-Process",
        "camwinc.exe`\"",
    ):
        assert forbidden.lower() not in text.lower(), forbidden


def test_only_the_probe_child_process_is_ever_killed():
    text = _text(MAIN)
    assert text.count(".Kill()") == 1
    assert "$proc.Kill()" in text


def test_main_script_is_careful_by_default():
    text = _text(MAIN)
    # Відкрита Sum3D зупиняє пробу без -AllowRunning; останнє місце ліцензії не займається.
    assert "$running -and -not $AllowRunning -and -not $DryRun" in text
    assert "$free -eq 1 -and -not $UseLastSeat" in text
    assert "$report.stopped = 'last seat'" in text
    # Проєкт копіюється, шлях до нього у звіт не йде.
    assert "Copy-Item -LiteralPath $Cam -Destination $workCam" in text
    assert "$report.cam_path" not in text and "cam = $Cam" not in text
    # Робоча тека процесу проби — своя, не тека Sum3D.
    assert "if (-not $CwdInSumDir) { $argList += @('-WorkDir'" in text
    # Суха перевірка виходить ДО знімків і завантаження DLL.
    assert text.index("DryRun: DLL не завантажувалась") < text.index("Get-Snapshot $SumDir")


def test_cmd_wrappers_never_use_risky_flags():
    dry = (KIT / "1_dry_run.cmd").read_text(encoding="utf-8")
    probe = (KIT / "2_probe.cmd").read_text(encoding="utf-8")
    assert "-DryRun" in dry
    for text in (dry, probe):
        assert "-UseLastSeat" not in text
        assert "-CwdInSumDir" not in text
    assert "-AllowRunning" in probe and '-Cam "%~1"' in probe


def test_app_never_runs_the_kit():
    # Набір згадують лише читач звітів і опис його інструмента MCP; жоден не вміє запускати процеси.
    mentions = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "sum3d-probe" in text or "sum3d_probe.ps1" in text or "sum3d_probe_step" in text or "2_probe.cmd" in text:
            mentions.append(path)
    assert sorted(p.name for p in mentions) == ["mcp_tools_sum3d_explore.py", "sum3d_probe_report.py"]
    for path in mentions:
        source = path.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "Popen", "startfile", "os.system", "ShellExecute", "CreateProcess"):
            assert forbidden not in source, f"{path.name}: {forbidden}"
    source = Path(spr.__file__).read_text(encoding="utf-8")
    for forbidden in ("import os", "write_text", "write_bytes", "open(", "unlink", "mkdir"):
        assert forbidden not in source, forbidden


# ---------- читач звітів ----------

REPORT = {
    "kit_version": 2,
    "started": "2026-11-17T13:05:00",
    "computer": "1-PC",
    "dry_run": False,
    "running_before": ["camwinc pid=4120 C:\\iCAMV5smart\\camwinc.exe", "camwinc pid=5200 C:\\iCAMV5smart\\camwinc.exe"],
    "candidates": [
        {"dir": "C:\\iCAMV5smart", "has_camwinc": True, "has_dll_x64": True, "dll_version": "1.0.0.1",
         "x64": True, "export_count": 231, "missing_exports": []},
        {"dir": "C:\\SUM3D\\MillBox", "has_camwinc": True, "has_dll_x64": False},
    ],
    "sum_dir": "C:\\iCAMV5smart",
    "dll": {"size": 20677120, "version": "1.0.0.1", "modified": "2024-06-14T14:29:35", "x64": True,
            "export_count": 231, "missing_exports": []},
    "camwinc_version": "6, 0, 0, 0",
    "cam_source_size": 8123456,
    "license": {"max_session": 4, "camwinc_running": 2},
    "cwd_in_sum_dir": False,
    "steps": [
        {"step": "load", "seconds": 1.2, "exit_code": 0, "result": {"step": "load", "ok": True, "loaded": True}},
        {"step": "init", "seconds": 3.4, "exit_code": 0, "result": {"step": "init", "ok": True, "init_code": 0}},
        {"step": "machines", "seconds": 2.0, "exit_code": 0,
         "result": {"step": "machines", "ok": True, "machines_raw": "350i_Boris|250i_Толік"}},
        {"step": "strategy", "seconds": 2.1, "exit_code": 0,
         "result": {"step": "strategy", "ok": True, "code": 0, "strategy": "Zirconium_V21"}},
        {"step": "space", "seconds": 90.0, "timeout": True},
    ],
    "sum_dir_changes_config": ["ЗМІНЕНО  \\CAMWINC.INI"],
    "sum_dir_changes_other": [f"НОВИЙ  \\tmp\\f{i}.tmp" for i in range(30)],
    "registry": {"keys_found": "HKCU\\Software\\LAB", "changed_sections": []},
    "new_processes": ["conhost pid=7000"],
    "sum3d_closed_during_probe": [],
    "sum3d_started_during_probe": [],
    "snapshot": {"files_before": 7000, "truncated_before": False, "files_after": 7030, "truncated_after": False},
    "finished": "2026-11-17T13:07:10",
}


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(spr, "reports_root", lambda: tmp_path)

    def write(name: str, data, *, bom: bool = True):
        folder = tmp_path / name
        folder.mkdir()
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        (folder / "report.json").write_text(text, encoding="utf-8-sig" if bom else "utf-8")

    return write


def test_no_reports_yet(reports):
    out = spr.report()
    assert out["звітів_усього"] == 0 and out["звіти"] == []
    assert out["набір"]["на_місці"] is True
    assert "sum3d_probe.ps1" in out["набір"]["файли"]


def test_report_summary(reports):
    reports("20261117_130500", REPORT)
    out = TOOLS_BY_NAME["kmill_sum3d_probe"].run(None, {})
    assert out["звітів_усього"] == 1
    r = out["звіти"][0]
    assert r["тека_sum3d"] == "C:\\iCAMV5smart"
    assert r["ліцензія"] == {"ліміт_сесій": 4, "відкрито_camwinc": 2}
    assert [s["крок"] for s in r["кроки"]] == ["load", "init", "machines", "strategy", "space"]
    assert r["кроки"][1]["код_initialize"] == 0 and r["кроки"][1]["ok"] is True
    # Кирилиця у значеннях зі звіту затерта.
    assert r["кроки"][2]["верстати"] == "350i_Boris|250i_·····"
    assert r["кроки"][3]["стратегія"] == "Zirconium_V21"
    assert r["кроки"][4] == {"крок": "space", "секунд": 90.0, "тайм_аут": True, "ok": False}
    assert r["інші_зміни"]["усього"] == 30 and len(r["інші_зміни"]["перші"]) == 20
    # PowerShell згорнув масив з одного елемента в рядок — читаємо як перелік.
    assert r["реєстр"]["гілки"] == ["HKCU\\Software\\LAB"]
    assert r["знайдені_теки"][1]["є_dll_x64"] is False
    assert r["sum3d_закрились"] == []
    assert "cam_source_size" not in json.dumps(r)


def test_dry_run_and_order_and_limit(reports):
    reports("20261117_120000", {"kit_version": 2, "started": "2026-11-17T12:00:00", "dry_run": True, "stopped": "dry run"})
    reports("20261117_130500", REPORT)
    reports("20261117_140000", {"started": "2026-11-17T14:00:00", "dry_run": False, "stopped": "last seat"})
    reports("not_a_report", REPORT)
    out = spr.report(limit=2)
    assert out["звітів_усього"] == 3
    assert [r["тека_звіту"] for r in out["звіти"]] == ["20261117_140000", "20261117_130500"]
    assert out["звіти"][0]["зупинено"] == "вільне лише останнє місце ліцензії"
    dry = spr.report(limit=3)["звіти"][2]
    assert dry["суха_перевірка"] is True and "зміни_налаштувань" not in dry


def test_broken_report_does_not_break_the_tool(reports):
    reports("20261117_150000", "{ not json")
    reports("20261117_160000", "[1, 2]")
    (spr.reports_root() / "20261117_170000").mkdir()
    out = spr.report(limit=5)
    assert [r.get("не_читається") for r in out["звіти"]] == ["FileNotFoundError", "ValueError", "JSONDecodeError"]
