"""Глибока розвідка Sum3D лише на читання (`sum3d_explore`, `kmill_sum3d_explore`)."""

from __future__ import annotations

import json
import os
import sys

import pytest

from app.services import sum3d_explore
from app.services.mcp_common import ToolError
from app.services.mcp_tools import TOOLS_BY_NAME
from app.shared_read import open_shared, read_shared


@pytest.fixture
def install(tmp_path, monkeypatch):
    root = tmp_path / "sum3d"
    (root / "Strategies" / "Zr").mkdir(parents=True)
    (root / "CAM-work").mkdir()
    (root / "Sum3D.exe").write_bytes(
        b"MZ\x00\x01" + b"Usage: Sum3D.exe /silent <file.blk>\x00\x02"
        + "exocad constructionInfo import".encode("utf-16-le") + b"\x00\x00\x03junk")
    (root / "Qt5Core.dll").write_bytes(b"MZ\x00 import everything here \x00")
    (root / "Strategies" / "Zr" / "crown_fine.xml").write_text(
        '<Strategy name="Crown fine"><Feed>1200</Feed><Spindle>42000</Spindle><Tool>Ball 2.0</Tool></Strategy>',
        encoding="utf-8")
    (root / "Strategies" / "Zr" / "bridge.xml").write_text(
        "<Strategy><Feed>900</Feed><Spindle>40000</Spindle><Tool>Ball 1.0</Tool><Note>Чорнова</Note></Strategy>",
        encoding="utf-8")
    (root / "Strategies" / "tools.ini").write_text("[T1]\nDiameter=2.0\nMaxRunTime = 600\nName=Фреза\n", encoding="cp1251")
    (root / "Strategies" / "Іваненко.str").write_bytes(b"TECN 2.0\x00\x00" + b"StepDown\x00" + bytes(range(40)))
    (root / "Strategies" / "Pacient_Petrenko.stl").write_bytes(b"solid x")
    (root / "CAM-work" / "2026-11-14_10-00-00.cam").write_bytes(b"secret project")
    monkeypatch.setattr(sum3d_explore, "roots", lambda db: [("нова", str(root))])
    monkeypatch.setattr(sum3d_explore, "_last", [])
    return root


def _run(**args):
    return TOOLS_BY_NAME["kmill_sum3d_explore"].run(None, args)


def test_map_counts_files_and_never_enters_work_folders(install):
    before = sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in install.rglob("*"))
    report = _run(what="map", root=1)
    by_dir = {row["тека"]: row for row in report["теки"]}
    assert report["файлів"] == 6, "без STL і без CAM-work"
    assert by_dir["Strategies/Zr"]["розширення"] == {".xml": 2}
    assert "CAM-work" not in json.dumps(report, ensure_ascii=False)
    assert sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in install.rglob("*")) == before, "нічого не змінено"


def test_listing_numbers_files_hides_stl_and_masks_names(install):
    report = _run(what="files", root=1, query="strategies")
    text = json.dumps(report, ensure_ascii=False)
    assert report["знайдено"] == 4 and [f["№"] for f in report["файли"]] == [1, 2, 3, 4]
    assert "Pacient" not in text and "Іваненко" not in text and "•" in text
    assert _run(what="files", root=1, ext="xml")["знайдено"] == 2


def test_schema_merges_keys_of_strategy_files(install):
    report = _run(what="schema", root=1, ext="xml")
    keys = {row["ключ"]: row for row in report["ключі"]}
    assert report["розібрано_текстових"] == 2 and report["двійкових"] == 0
    assert keys["Feed"]["числа"] == [900.0, 1200.0] and keys["Feed"]["разів"] == 2
    assert keys["Spindle"]["числа"] == [40000.0, 42000.0]
    assert set(keys["Tool"]["приклади"]) == {"Ball 2.0", "Ball 1.0"} and keys["Tool"]["числа"] is None
    assert keys["name"]["приклади"] == ["Crown fine"], "атрибути теж ключі"
    assert "Чорнова" not in json.dumps(report, ensure_ascii=False)
    with pytest.raises(ToolError, match="вкажіть `ext`"):
        _run(what="schema", root=1)


def test_file_by_number_text_and_binary(install):
    listing = _run(what="files", root=1, ext="ini")
    assert listing["знайдено"] == 1
    text = _run(what="file", file=1)
    assert text["вид"] == "текст" and text["рядки"][0] == "[T1]"
    assert text["числові_ключі"] == ["Diameter = 2.0", "MaxRunTime = 600"]
    assert "Фреза" not in json.dumps(text, ensure_ascii=False)

    _run(what="files", root=1, ext="str")
    binary = _run(what="file", file=1)
    assert binary["вид"] == "двійковий" and binary["заголовок_hex"].startswith("54 45 43 4e")
    assert "StepDown" in binary["рядки_в_ньому"] and "Іваненко" not in json.dumps(binary, ensure_ascii=False)
    with pytest.raises(ToolError, match="в останньому переліку немає"):
        _run(what="file", file=9)


def test_file_needs_a_listing_first(install):
    with pytest.raises(ToolError, match="спершу перелік"):
        _run(what="file", file=1)
    with pytest.raises(ToolError, match="потрібен `file`"):
        _run(what="file")


def test_exe_strings_only_from_own_binaries(install):
    report = _run(what="exe", root=1)
    files = {row["файл"]: row for row in report["файли"]}
    hints = files["Sum3D.exe"]["рядки"]
    assert any("/silent" in s for s in hints["/silent"]) and ".blk" in hints
    assert hints["exocad"] == ["exocad constructionInfo import"], "рядок UTF-16 теж знайдено"
    assert "рядки" not in files["Qt5Core.dll"], "чужа бібліотека: лише назва й версія"


def test_roots_and_bad_arguments_are_explained(install, monkeypatch):
    assert _run()["теки"][0]["назва"] == "нова"
    with pytest.raises(ToolError, match="номер теки 1…1"):
        _run(what="map", root=3)
    with pytest.raises(ToolError, match="`what` — roots"):
        _run(what="delete")
    monkeypatch.setattr(sum3d_explore, "roots", lambda db: [])
    assert "не задано" in _run()["теки"]
    with pytest.raises(ToolError, match="тек Sum3D не задано"):
        _run(what="files")


def test_associations_never_fail():
    report = sum3d_explore.associations()
    assert "асоціації" in report or "помилка" in report


def test_roots_come_from_settings_without_duplicates(db_session, tmp_path, monkeypatch):
    from app.services import sum3d_deep, sum3d_install
    from app.settings_store import set_setting

    one, two = tmp_path / "A", tmp_path / "B"
    set_setting(db_session, sum3d_install.INSTALLS_KEY,
                sum3d_install.installs_json([sum3d_install.Install("нова", str(one))]))
    set_setting(db_session, sum3d_install.STRATEGIES_KEY, str(two))
    monkeypatch.setattr(sum3d_deep, "settings_roots", lambda blanks: [str(one), str(tmp_path / "C")])
    assert [name for name, _p in sum3d_explore.roots(db_session)] == ["нова", "стратегії", "тека Sum3D"]


@pytest.mark.skipif(sys.platform != "win32", reason="блокування файлів — особливість Windows")
def test_shared_read_lets_the_owner_rename_and_delete(tmp_path):
    target = tmp_path / "strategy.xml"
    target.write_bytes(b"<Feed>1200</Feed>")
    with open(target, "rb"):
        with pytest.raises(PermissionError):
            os.replace(target, tmp_path / "blocked.xml")
    with open_shared(str(target)) as handle:
        os.replace(target, tmp_path / "saved.xml")
        assert handle.read() == b"<Feed>1200</Feed>"
    with open_shared(str(tmp_path / "saved.xml")):
        os.remove(tmp_path / "saved.xml")
    assert not (tmp_path / "saved.xml").exists()
    with pytest.raises(OSError):
        read_shared(str(tmp_path / "missing.xml"), 10)
