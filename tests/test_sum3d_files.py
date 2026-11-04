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
import zlib

import pytest

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


def _round(found: dict, range_name: str = "коефіцієнт_1_10_до_1_40") -> list[tuple[str, float]]:
    return [(g["тип"], g["значення"]) for g in found["круглі_числа"][range_name]]


def test_binary_round_double_without_marker_is_found(tmp_path):
    """Прод 03.11.26: у `.blk` жодного слова-маркера. Коефіцієнт, введений
    руками, — кругле число; координата сітки — ні."""
    path = tmp_path / "zr14_14-Monolith-A3-x96.blk"
    noise = struct.pack("<3f", 1.2345678, 12.987654, -3.1415927)
    payload = (
        b"<SUM3D_BLANK V1.0>\x00\x07" + noise
        + struct.pack("<d", 1.25) + noise + struct.pack(">d", 0.8)
    )
    path.write_bytes(payload)
    found = sum3d_files.describe_file(str(path))
    assert found["заголовок_текст"] == "<SUM3D_BLANK V1.0>"
    assert ("double LE", 1.25) in _round(found)
    assert ("double BE", 0.8) in _round(found, "обернений_0_71_до_0_91")
    assert all(value != 1.2345678 for _t, value in _round(found))
    group = next(g for g in found["круглі_числа"]["коефіцієнт_1_10_до_1_40"] if g["тип"] == "double LE")
    assert group["разів"] == 1 and group["зсуви"] == [20 + len(noise)]


def test_utf16be_marker_reads_big_endian_number(tmp_path):
    path = tmp_path / "q.blk"
    word = "ScaleFactor".encode("utf-16-be")
    path.write_bytes(b"\x00\x01\x02" + word + struct.pack(">d", 1.2512) + b"\x00" * 8)
    found = sum3d_files.describe_file(str(path))
    # Той самий рядок, зсунутий на байт, читається й як UTF-16LE («ScaleFacto»):
    # кодування не вгадуємо, віддаємо обидва прочитання.
    [marker] = [m for m in found["маркери"] if m["кодування"] == "utf-16-be"]
    assert marker["маркер"] == "ScaleFactor"
    assert {"тип": "double", "зсув": 0, "значення": 1.2512} in marker["кандидати"]


def test_zlib_stream_inside_binary_is_unpacked(tmp_path):
    path = tmp_path / "2026-11-03_13-12-29.cam"
    inner = zlib.compress(
        b"\x00\x01" + b"ShrinkFactor\x00" + struct.pack("<d", 1.247) + PATIENT.encode() + b"\x00" * 32
    )
    path.write_bytes(b"TECN 2.0\x00\x00" + os.urandom(64).replace(b"\x78", b"\x00") + inner + b"\x00" * 8)
    found = sum3d_files.describe_file(str(path))
    assert found["заголовок_текст"] == "TECN 2.0"
    [stream] = found["стиснуті_потоки"]
    assert stream["зсув"] == 74 and stream["стиснуто"] == len(inner)
    [marker] = stream["вміст"]["маркери"]
    assert {"тип": "double", "зсув": 1, "значення": 1.247} in marker["кандидати"]
    assert PATIENT not in json.dumps(found, ensure_ascii=False)


def test_large_binary_is_not_scanned_whole(tmp_path):
    path = tmp_path / "big.cam"
    path.write_bytes(struct.pack("<d", 1.25) * (sum3d_files.ROUND_SCAN_MAX_BYTES // 8 + 1))
    found = sum3d_files.describe_file(str(path))
    assert isinstance(found["круглі_числа"], str)


def test_common_words_drop_per_disc_names(tmp_path):
    paths = []
    for i, name in enumerate((PATIENT, "Shevchenko_Olena", "Koval_Ivan")):
        path = tmp_path / f"{i}.blk"
        packed = zlib.compress(b"\x00ShrinkScale\x00" + name.encode() + b"\x00")
        path.write_bytes(b"\x01" + "BlankHeight".encode("utf-16-le") + b"\x00" + packed)
        paths.append(str(path))
    words = sum3d_files.common_words(paths)
    assert "BlankHeight" in words and "ShrinkScale" in words
    assert not any(n in words for n in (PATIENT, "Shevchenko_Olena", "Koval_Ivan"))


def _blank_head(shrink_le_float: float, filler: bytes) -> bytes:
    return b"<SUM3D_BLANK V1.2>\x00\x00" + struct.pack("<f", 98.5) + struct.pack("<f", shrink_le_float) + filler


def test_compare_headers_finds_pmma_one_against_zirconia_shrink(tmp_path):
    """У ПММА усадки немає: де в ній рівно 1.0, а в цирконії 1.1–1.4 — там
    Sum3D і тримає коефіцієнт, хай навіть некруглий (1.2365)."""
    files = []
    for name, shrink in (("pmma16_16-a1-x93.blk", 1.0), ("zr25_25-Monolith-A1-x28.blk", 1.2365),
                         ("zr14_14-Emotions-A1-x264.blk", 1.2412)):
        path = tmp_path / name
        path.write_bytes(_blank_head(shrink, os.urandom(32)))
        files.append((name, str(path)))
    result = sum3d_files.compare_headers(files)
    [row] = [r for r in result["одиниця_проти_усадки"] if r["тип"] == "float LE" and r["зсув"] == 24]
    assert row["значення"]["pmma16_16-a1-x93.blk"] == 1.0
    assert row["значення"]["zr25_25-Monolith-A1-x28.blk"] == pytest.approx(1.2365, abs=1e-6)
    # Однаковий діаметр 98.5 у всіх — не кандидат.
    assert all(r["зсув"] != 20 for r in result["різні_числа_0_5_до_2"])


def test_several_brands_take_newest_of_each(tmp_path):
    folder = tmp_path / "blanks" / "Zr" / "16"
    folder.mkdir(parents=True)
    names = ["pmma16_16-a1-x1.blk", "pmma16_16-a1-x2.blk", "pmma16_16-a1-x3.blk",
             "zr16_16-Monolith-A1-x1.blk"]
    for i, name in enumerate(names):
        (folder / name).write_bytes(b"x")
        os.utime(folder / name, (1_000_000 + i, 1_000_000 + i))
    found = sum3d_files.newest_blanks(str(tmp_path / "blanks"), brand="pmma, monolith", limit=2)
    assert [f.name for f in found] == ["pmma16_16-a1-x3.blk", "pmma16_16-a1-x2.blk",
                                       "zr16_16-Monolith-A1-x1.blk"]


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
