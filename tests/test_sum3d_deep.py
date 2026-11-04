"""Розвідка Sum3D поза файлами дисків: налаштування, реєстр, проєкт `.cam`.

Формату знову не знаємо, тож тести тримають обіцянки: масштаб видно і під
поворотом, вершини розрізняються «як є» і «помножені», назви STL та імена
пацієнтів назовні не виходять.
"""

from __future__ import annotations

import json
import math
import os
import struct

from app.services import sum3d_deep
from app.settings_store import set_setting
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

PATIENT = "Ivanenko_Petro"


def _rotated_scale(k: float, angle: float = 0.6) -> list[float]:
    c, s = math.cos(angle), math.sin(angle)
    return [
        k * c, -k * s, 0.0, 12.5,
        k * s, k * c, 0.0, -3.25,
        0.0, 0.0, k, 7.0,
        0.0, 0.0, 0.0, 1.0,
    ]


# ── Налаштування ────────────────────────────────────────────────────────────


def test_marked_lines_keep_numbers_and_drop_paths_and_cyrillic():
    lines = sum3d_deep.marked_lines(
        "[Blank]\n"
        "ShrinkFactor=1.25\n"
        "Диск Monolith 1,247\n"
        f"Recent=C:\\work\\{PATIENT}_scale.stl\n"
        f"last={PATIENT}.STL scale\n"
        "other=1\n"
    )
    assert lines == ["ShrinkFactor = 1.25", "Monolith 1,247"]


def test_scan_settings_finds_factor_and_masks_names(tmp_path):
    root = tmp_path / "sum"
    (root / "DENTAL" / "materials").mkdir(parents=True)
    (root / "DENTAL" / "materials" / "zirconia.ini").write_text("Name=Monolith\nScale=1.25\n", encoding="utf-8")
    (root / "DENTAL" / "CAM-blanks").mkdir()
    (root / "DENTAL" / "CAM-blanks" / "a.ini").write_text("Scale=9\n", encoding="utf-8")
    (root / "DENTAL" / f"{PATIENT} кейс").mkdir()
    (root / "DENTAL" / f"{PATIENT} кейс" / "x.ini").write_text("Scale=1.3\n", encoding="utf-8")
    (root / "DENTAL" / "crown.stl").write_bytes(b"Scale=1.5")
    (root / "DENTAL" / "lib.dat").write_bytes(
        b"\x00\x01ShrinkFactor\x00" + struct.pack("<d", 1.2365) + b"\x00" * 8
    )

    found = sum3d_deep.scan_settings([str(root)])

    by_file = {f["файл"]: f for f in found["знахідки"]}
    assert by_file["sum/DENTAL/materials/zirconia.ini"]["рядки"] == ["Name = Monolith", "Scale = 1.25"]
    assert not any("CAM-blanks" in name or "crown" in name for name in by_file)
    assert "sum/DENTAL/<…>/x.ini" in by_file
    [marker] = by_file["sum/DENTAL/lib.dat"]["маркери"]
    assert {"тип": "double", "зсув": 1, "значення": 1.2365} in marker["кандидати"]
    assert PATIENT not in json.dumps(found, ensure_ascii=False)


def test_settings_roots_go_above_blanks_but_not_to_drive_root(tmp_path, monkeypatch):
    for variable in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "Sum3D Dental").mkdir()
    (tmp_path / "Other").mkdir()
    deep = str(tmp_path / "sum" / "DENTAL" / "CAM-blanks")
    assert sum3d_deep.settings_roots(deep) == [str(tmp_path / "sum"), str(tmp_path / "Sum3D Dental")]
    shallow = os.path.join(os.path.abspath(os.sep), "DENTAL", "CAM-blanks")
    assert sum3d_deep.settings_roots(shallow)[0] == os.path.join(os.path.abspath(os.sep), "DENTAL")


def test_registry_gives_only_numbers_under_markers(monkeypatch):
    values = [
        ("HKCU\\Software\\Sum3D\\Blank", "ShrinkFactor", "1,25"),
        ("HKCU\\Software\\Sum3D\\Blank", "Unnamed", 1.3),
        ("HKCU\\Software\\Sum3D\\Monolith", "Value", 1),
        ("HKCU\\Software\\Sum3D\\Window", "Width", 800),
        ("HKCU\\Software\\Sum3D\\Recent", "ScaleFile", f"C:\\{PATIENT}.stl"),
    ]
    monkeypatch.setattr(sum3d_deep, "_registry_values", lambda: iter(values))
    found = sum3d_deep.scan_registry()
    assert [(h["назва"], h["значення"]) for h in found["значень"]] == [
        ("ShrinkFactor", 1.25), ("Unnamed", 1.3), ("Value", 1.0),
    ]
    assert PATIENT not in json.dumps(found, ensure_ascii=False)


def test_real_registry_walk_does_not_fail():
    assert isinstance(sum3d_deep.scan_registry()["усього"], int)


# ── Проєкт .cam ─────────────────────────────────────────────────────────────


def test_affine_scale_is_seen_through_rotation():
    data = b"\x01" * 13 + struct.pack("<16d", *_rotated_scale(1.2365)) + b"\x02" * 16
    [group] = sum3d_deep.affine_scales(data)
    assert group["тип"] == "double LE"
    assert group["масштаб"] == [1.2365, 1.2365, 1.2365]
    assert group["перший_зсув"] == 13


def test_affine_scale_column_major_and_float():
    m = _rotated_scale(1.25)
    column_major = [m[i + j * 4] for i in range(4) for j in range(4)]
    data = struct.pack("<16f", *column_major)
    groups = sum3d_deep.affine_scales(data)
    assert any(g["тип"] == "float LE" and g["масштаб"][0] == 1.25 for g in groups)


def test_identity_and_implausible_matrices():
    identity = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    [group] = sum3d_deep.affine_scales(struct.pack("<16d", *identity))
    assert group["масштаб"] == [1.0, 1.0, 1.0]
    huge = _rotated_scale(40.0)
    assert sum3d_deep.affine_scales(struct.pack("<16d", *huge)) == []


def test_equal_triples_find_scale_vector():
    data = b"\x07\x07\x07" + struct.pack(">3d", 1.2412, 1.2412, 1.2412) + struct.pack("<3f", 1.3, 1.1, 1.2)
    found = sum3d_deep.equal_triples(data)
    assert {"тип": "double BE", "значення": 1.2412, "разів": 1, "перший_зсув": 3} in found
    assert not any(g["тип"] == "float LE" for g in found)


def test_exact_value_gives_neighbours():
    data = struct.pack("<5d", 0.0, 2.0, 1.25, 3.0, 4.0) + struct.pack("<8d", *([0.0] * 8))
    found = sum3d_deep.exact_value(data, 1.25)
    assert found["double LE"]["разів"] == 1
    assert found["double LE"]["приклади"][0]["зсув"] == 16
    assert found["double LE"]["приклади"][0]["оточення"][:4] == [0.0, 2.0, 1.25, 3.0]
    assert found["float LE"]["разів"] == 0


def _stl(path, vertices: list[tuple[float, float, float]]) -> str:
    facets = b""
    for i in range(0, len(vertices), 3):
        a, b, c = vertices[i:i + 3]
        facets += struct.pack("<12fH", 0, 0, 1, *a, *b, *c, 0)
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(facets) // 50) + facets)
    return str(path)


def test_vertices_as_is_and_scaled_are_told_apart(tmp_path):
    vertices = [(1.5, 2.5, 3.5), (4.5, 5.5, 6.5), (7.5, 8.5, 9.5)]
    stl = _stl(tmp_path / f"{PATIENT}_cad.stl", vertices)
    sampled = sum3d_deep.sample_vertices([stl])
    assert sampled == [vertices[0]]

    as_is = struct.pack("<3f", *vertices[0])
    scaled = struct.pack("<3f", *(c * 1.25 for c in vertices[0]))
    assert sum3d_deep.vertex_presence(b"xx" + as_is, sampled, 1.25)["як_є_float"] == 1
    result = sum3d_deep.vertex_presence(b"xx" + scaled, sampled, 1.25)
    assert result["як_є_float"] == 0 and result["помножені_float"] == 1


def test_probe_cam_says_what_it_found_without_names(tmp_path):
    stl = _stl(tmp_path / f"{PATIENT}_cad.stl", [(1.5, 2.5, 3.5), (4.5, 5.5, 6.5), (7.5, 8.5, 9.5)])
    cam = tmp_path / "2026-11-03_13-12-29.cam"
    cam.write_bytes(
        b"TECN 2.0\x00\x00" + PATIENT.encode() + b"\x00"
        + struct.pack("<16d", *_rotated_scale(1.25)) + struct.pack("<3f", 1.5, 2.5, 3.5)
    )
    found = sum3d_deep.probe_cam(str(cam), stl_paths=[stl], expected=1.25)
    assert found["матриці_4х4"][0]["масштаб"] == [1.25, 1.25, 1.25]
    assert found["вершини_stl"]["як_є_float"] == 1
    assert PATIENT not in json.dumps(found, ensure_ascii=False)


def test_unreadable_project_is_reported(tmp_path):
    assert "помилка" in sum3d_deep.probe_cam(str(tmp_path / "нема.cam"))


# ── ПММА проти цирконію ─────────────────────────────────────────────────────


def _project(path, factor: float, filler: bytes) -> tuple[str, str]:
    path.write_bytes(b"TECN 2.0\x00\x00" + struct.pack("<d", 98.5) + struct.pack("<d", factor) + filler)
    return path.name, str(path)


def test_compare_groups_needs_every_file_to_agree(tmp_path):
    plain = [_project(tmp_path / "p1.cam", 1.0, b"\x11" * 64), _project(tmp_path / "p2.cam", 1.0, b"\x22" * 64)]
    shrunk = [_project(tmp_path / "z1.cam", 1.25, b"\x33" * 64), _project(tmp_path / "z2.cam", 1.2365, b"\x44" * 64)]
    found = sum3d_deep.compare_groups(plain, shrunk)
    [place] = [r for r in found["місця"] if r["тип"] == "double LE"]
    assert place["зсув"] == 18
    assert place["значення"] == {"z1.cam": 1.25, "z2.cam": 1.2365}

    # Один цирконієвий файл без коефіцієнта на цьому місці — не кандидат.
    odd = [*shrunk, _project(tmp_path / "z3.cam", 3.0, b"\x55" * 64)]
    assert not any(r["зсув"] == 18 for r in sum3d_deep.compare_groups(plain, odd)["місця"])


def test_compare_groups_without_one_side_says_so(tmp_path):
    found = sum3d_deep.compare_groups([], [_project(tmp_path / "z.cam", 1.25, b"")])
    assert "примітка" in found


# ── Інструмент ──────────────────────────────────────────────────────────────


def _call(app, args: dict) -> dict:
    status, _, body = MiniClient(app).post_json(
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "kmill_sum3d_files", "arguments": args}},
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    assert status == 200, body
    return json.loads(body)["result"]


def _configure(session_factory, tmp_path):
    """Диски, проєкти, робота цирконію з STL і робота ПММА."""
    from app.material_catalog import ensure_seeded, material_id_by_name
    from app.models import Order

    (tmp_path / "sum" / "DENTAL" / "CAM-blanks").mkdir(parents=True)
    (tmp_path / "sum" / "DENTAL" / "m.ini").write_text("Monolith=1.25\n", encoding="utf-8")
    projects = tmp_path / "cam-work"
    projects.mkdir()
    _project(projects / "2026-11-03_13-12-29.cam", 1.25, b"\x33" * 64)
    _project(projects / "2026-11-03_09-00-00.cam", 1.0, b"\x11" * 64)
    job = tmp_path / "tech" / "2026-11-02_00004-018"
    job.mkdir(parents=True)
    _stl(job / f"{PATIENT}-25-26-bridge_cad.stl", [(1.5, 2.5, 3.5), (4.5, 5.5, 6.5), (7.5, 8.5, 9.5)])
    with session_factory() as db:
        ensure_seeded(db)
        mats = material_id_by_name(db)
        set_setting(db, "cam_blanks_path", str(tmp_path / "sum" / "DENTAL" / "CAM-blanks"))
        set_setting(db, "sum3d_projects_path", str(projects))
        set_setting(db, "technician_files_path", str(tmp_path / "tech"))
        db.add(Order(source="lab", status="прораховано", sheet_tab="03.11.26", sum3d_id="13-12-29",
                     job_code="2026-11-02_00004-018", material_id=mats["Цирконій"]))
        # Той самий хвіст Sum3D, але інший місяць: його STL сюди не беремо.
        db.add(Order(source="lab", status="прораховано", sheet_tab="03.10.26", sum3d_id="13-12-29",
                     job_code="нема", material_id=mats["Цирконій"]))
        db.add(Order(source="lab", status="прораховано", sheet_tab="03.11.26", sum3d_id="09-00-00",
                     material_id=mats["ПММА"]))
        db.commit()


def test_tool_settings_kind(app_db, tmp_path):  # noqa: F811
    app, session_factory = app_db
    _configure(session_factory, tmp_path)
    data = _call(app, {"kind": "settings"})["structuredContent"]
    [finding] = [f for f in data["налаштування"]["знахідки"] if f["файл"].endswith("m.ini")]
    assert finding["рядки"] == ["Monolith = 1.25"]
    assert "значень" in data["реєстр"]


def test_tool_cam_deep_kind(app_db, tmp_path):  # noqa: F811
    app, session_factory = app_db
    _configure(session_factory, tmp_path)
    data = _call(app, {"kind": "cam_deep", "sum3d_id": "13-12-29", "value": 1.25})["structuredContent"]
    [project] = data["проєкти"]
    assert project["файл"] == "2026-11-03_13-12-29.cam"
    assert project["точне_значення"]["double LE"]["разів"] == 1
    assert project["вершини_stl"]["перевірено"] == 1
    assert PATIENT not in json.dumps(data, ensure_ascii=False)


def test_tool_cam_deep_requires_id_and_numeric_value(app_db, tmp_path):  # noqa: F811
    app, session_factory = app_db
    _configure(session_factory, tmp_path)
    assert _call(app, {"kind": "cam_deep"})["isError"] is True
    assert _call(app, {"kind": "cam_deep", "sum3d_id": "13-12-29", "value": "1.25"})["isError"] is True


def test_tool_cam_compare_takes_family_from_the_work(app_db, tmp_path):  # noqa: F811
    app, session_factory = app_db
    _configure(session_factory, tmp_path)
    data = _call(app, {"kind": "cam_compare", "limit": 3})["structuredContent"]
    assert data["без_усадки"] == ["2026-11-03_09-00-00.cam"]
    assert data["з_усадкою"] == ["2026-11-03_13-12-29.cam"]
    assert any(p["зсув"] == 18 and p["тип"] == "double LE" for p in data["місця"])


def test_tool_rejects_unknown_kind_and_names_new_ones(app_db):  # noqa: F811
    app, _ = app_db
    result = _call(app, {"kind": "stl"})
    assert result["isError"] is True
    assert "cam_deep" in result["content"][0]["text"]
