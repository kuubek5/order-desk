"""`kmill_sum3d_files` — що Sum3D пише у `.blk`/`.cam`, без імен пацієнтів.

Формату файлів Sum3D ми ще не бачили, тож тести тримають не формат, а дві
обіцянки інструмента: (1) коефіцієнт знаходиться, хоч би як він лежав — XML,
ключ = значення, архів, бінарний double біля маркера; (2) текстові значення й
назви STL назовні не виходять ніколи (у проєкті — імена пацієнтів).
"""

from __future__ import annotations

import io
import json
import os
import struct
import zipfile

from app.services import sum3d_files
from app.settings_store import set_setting
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

PATIENT = "Ivanenko_Petro"


def _numbers(found: dict) -> list[float]:
    return [n["значення"] for n in found["числа_біля_маркерів"]]


def test_xml_blank_gives_factor_but_not_text(tmp_path):
    path = tmp_path / "25-Monolith-A3-x51.blk"
    path.write_text(
        "<Blank><Name>Monolith A3</Name><Lot>Z1234</Lot>"
        '<Shrinkage factor="1.2512"/><Height>25</Height>'
        f"<Owner>{PATIENT}</Owner></Blank>",
        encoding="utf-8",
    )
    found = sum3d_files.describe_file(str(path))
    assert found["формат"] == "xml"
    assert 1.2512 in _numbers(found)
    assert {"ключ": "Shrinkage@factor", "значення": 1.2512} in found["числа_1_10_до_1_40"]
    dumped = json.dumps(found, ensure_ascii=False)
    assert PATIENT not in dumped and "Z1234" not in dumped and "Monolith A3" not in dumped


def test_key_value_text(tmp_path):
    path = tmp_path / "a.blk"
    path.write_text(f"[Blank]\nName={PATIENT}\nScaleFactor=1,247\nHeight=20\n", encoding="cp1251")
    found = sum3d_files.describe_file(str(path))
    assert 1.247 in _numbers(found)
    assert "ScaleFactor" in found["ключі"]
    assert PATIENT not in json.dumps(found, ensure_ascii=False)


def test_zip_project_hides_stl_names_and_reads_inner_xml(tmp_path):
    path = tmp_path / "2026-11-03_13-12-29.cam"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("project.xml", '<Project><Blank scale="1.25"/></Project>')
        archive.writestr(f"{PATIENT}_crown.stl", b"solid x\n" + b"\x00" * 64)
        archive.writestr(f"{PATIENT} notes.txt", "Scale=1.3")
    path.write_bytes(buf.getvalue())
    found = sum3d_files.describe_file(str(path))
    assert found["формат"] == "zip"
    names = [e["назва"] for e in found["вміст"]]
    assert names[0] == "project.xml"
    assert names[1] == "<файл 2>.stl" and names[2] == "<файл 3>.txt"
    assert 1.25 in _numbers(found["вміст"][0])
    assert PATIENT not in json.dumps(found, ensure_ascii=False)


def test_binary_double_after_marker(tmp_path):
    path = tmp_path / "b.blk"
    payload = (
        b"\x01\x02BLK\x00" + PATIENT.encode() + b"\x00"
        + b"ShrinkFactor\x00" + struct.pack("<d", 1.2512) + b"\x00" * 16
    )
    path.write_bytes(payload)
    found = sum3d_files.describe_file(str(path))
    assert found["формат"] == "бінарний"
    [marker] = found["маркери"]
    assert marker["маркер"] == "ShrinkFactor"
    assert {"тип": "double", "зсув": 1, "значення": 1.2512} in marker["кандидати"]
    assert PATIENT not in json.dumps(found, ensure_ascii=False)


def test_find_newest_blanks_and_project_by_id(tmp_path):
    blanks = tmp_path / "blanks"
    (blanks / "Zr" / "25").mkdir(parents=True)
    (blanks / "Zr" / "20").mkdir(parents=True)
    old = blanks / "Zr" / "25" / "25-Monolith-A3-x50.blk"
    new = blanks / "Zr" / "20" / "20-Monolith-A2-x07.blk"
    emo = blanks / "Zr" / "20" / "20-Emotions-A1-x02.blk"
    for i, p in enumerate((old, emo, new)):
        p.write_text("Scale=1.25")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
    assert [f.name for f in sum3d_files.newest_blanks(str(blanks), limit=5)] == [
        new.name, emo.name, old.name,
    ]
    assert [f.name for f in sum3d_files.newest_blanks(str(blanks), brand="MONO", limit=5)] == [
        new.name, old.name,
    ]

    projects = tmp_path / "cam-work"
    projects.mkdir()
    (projects / "2026-11-03_13-12-29.cam").write_text("x")
    (projects / "2026-11-03_14-00-00.cam").write_text("x")
    (projects / "2026-11-03_13-12-29.stl").write_text("x")
    found = sum3d_files.newest_projects(str(projects), sum3d_id="13-12-29", limit=5)
    assert [f.name for f in found] == ["2026-11-03_13-12-29.cam"]


def test_missing_folder_is_empty_not_error(tmp_path):
    assert sum3d_files.newest_blanks(str(tmp_path / "нема")) == []
    assert sum3d_files.newest_projects(str(tmp_path / "нема")) == []


def _call_tool(app, args: dict) -> dict:
    status, _, body = MiniClient(app).post_json(
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "kmill_sum3d_files", "arguments": args}},
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    assert status == 200, body
    return json.loads(body)["result"]


def test_tool_reads_configured_folders(app_db, tmp_path):  # noqa: F811
    app, session_factory = app_db
    blanks = tmp_path / "blanks" / "Zr" / "25"
    blanks.mkdir(parents=True)
    (blanks / "25-Monolith-A3-x51.blk").write_text("<B><Shrink>1.25</Shrink></B>")
    projects = tmp_path / "cam-work"
    projects.mkdir()
    (projects / "2026-11-03_13-12-29.cam").write_text("Scale=1.25\n")
    with session_factory() as db:
        set_setting(db, "cam_blanks_path", str(tmp_path / "blanks"))
        set_setting(db, "sum3d_projects_path", str(projects))
        db.commit()
    result = _call_tool(app, {"sum3d_id": "13-12-29"})
    data = result["structuredContent"]
    assert data["диски"][0]["файл"] == "25-Monolith-A3-x51.blk"
    assert data["диски"][0]["числа_біля_маркерів"] == [{"ключ": "Shrink", "значення": 1.25}]
    assert data["проєкти"][0]["файл"] == "2026-11-03_13-12-29.cam"


def test_tool_without_folders_says_where_to_set(app_db):  # noqa: F811
    app, _ = app_db
    data = _call_tool(app, {})["structuredContent"]
    assert "Нові диски" in data["диски"]
    assert "Шляхи" in data["проєкти"]


def test_tool_rejects_unknown_kind(app_db):  # noqa: F811
    app, _ = app_db
    result = _call_tool(app, {"kind": "stl"})
    assert result["isError"] is True
