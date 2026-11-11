"""STL робіт для 3D на табло (`app/services/stl_shapes.py`, `kmill_stl_shapes`).

Справжні моделі тут не лежать (форма робіт пацієнтів); перевіряємо механіку на
синтетичних тілах, а вигляд — інструментом MCP на цеховому ПК.
"""

from __future__ import annotations

import struct

import numpy as np

from app.services import stl_shapes
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура

_CUBE = np.array([
    # кожна грань двома трикутниками, обхід назовні
    [[0, 0, 0], [1, 1, 0], [1, 0, 0]], [[0, 0, 0], [0, 1, 0], [1, 1, 0]],
    [[0, 0, 1], [1, 0, 1], [1, 1, 1]], [[0, 0, 1], [1, 1, 1], [0, 1, 1]],
    [[0, 0, 0], [1, 0, 0], [1, 0, 1]], [[0, 0, 0], [1, 0, 1], [0, 0, 1]],
    [[0, 1, 0], [1, 1, 1], [1, 1, 0]], [[0, 1, 0], [0, 1, 1], [1, 1, 1]],
    [[0, 0, 0], [0, 0, 1], [0, 1, 1]], [[0, 0, 0], [0, 1, 1], [0, 1, 0]],
    [[1, 0, 0], [1, 1, 0], [1, 1, 1]], [[1, 0, 0], [1, 1, 1], [1, 0, 1]],
], dtype=np.float64) * 8.0


def _write_binary(path, tris):
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            fh.write(struct.pack("<12fH", 0, 0, 0, *t.reshape(-1).tolist(), 0))


def _write_ascii(path, tris):
    lines = ["solid x"]
    for t in tris:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += [f"vertex {p[0]} {p[1]} {p[2]}" for p in t]
        lines += ["endloop", "endfacet"]
    path.write_text("\n".join(lines + ["endsolid x"]), encoding="ascii")


def test_reads_binary_and_ascii_alike(tmp_path):
    b, a = tmp_path / "b.stl", tmp_path / "a.stl"
    _write_binary(b, _CUBE)
    _write_ascii(a, _CUBE)
    assert np.allclose(stl_shapes.read_stl(b), _CUBE)
    assert np.allclose(stl_shapes.read_stl(a), _CUBE)
    (tmp_path / "junk.stl").write_bytes(b"not a mesh")
    assert stl_shapes.read_stl(tmp_path / "junk.stl") is None


def test_closed_and_open_models():
    assert stl_shapes.open_edge_count(_CUBE) == 0
    assert stl_shapes.open_edge_count(_CUBE[1:]) == 3, "без одного трикутника — три ребра без пари"


def test_describe_gives_measures_and_both_poses_without_the_name(tmp_path):
    path = tmp_path / "Іваненко-36-crown_cad.stl"
    _write_binary(path, _CUBE)
    info, poses = stl_shapes.describe(path)
    assert info["прочитано"] and info["замкнена"] and info["трикутників"] == 12
    assert info["exocad"] is True and info["тип_exocad"] == "crown"
    assert info["габарити_у_файлі_мм"] == [8.0, 8.0, 8.0]
    assert "Іваненко" not in repr(info)
    assert poses is not None and len(poses) == 2
    image = stl_shapes.render(poses[0], label="р1·ф1 файл")
    assert image.size == (240, 240)


def test_mcp_tool_lists_kinds_then_shows_a_work(app_db, tmp_path, monkeypatch):  # noqa: F811
    from app.models import Order
    from app.services.mcp_tools import TOOLS_BY_NAME

    _, factory = app_db
    folder = tmp_path / "Клієнт Петренко" / "11.11.26" / "титан"
    folder.mkdir(parents=True)
    _write_binary(folder / "Петренко-46-abutment.stl", _CUBE)
    with factory() as db:
        db.add(Order(source="lab", client_name="Петренко", kind="абатмент гвинтовий",
                     material_color="титан", quantity=1))
        db.add(Order(source="lab", client_name="Інший", kind="анатомія", material_color="mono a2", quantity=2))
        db.commit()
    monkeypatch.setattr("app.services.stl_height.work_folder", lambda order, tech, export: folder)
    tool = TOOLS_BY_NAME["kmill_stl_shapes"]
    with factory() as db:
        kinds = tool.run(db, {})
        assert kinds["види_робіт"] == {"абатмент гвинтовий": 1, "анатомія": 1}
        result = tool.run(db, {"kind": "абатмент"})
    assert result["кандидатів"] == 1 and len(result["роботи"]) == 1
    work = result["роботи"][0]
    assert work["вид_роботи"] == "абатмент гвинтовий" and work["файли"][0]["замкнена"] is True
    image = result.pop("_image")
    assert image["mimeType"] == "image/png" and image["data"]
    text = repr(result)
    assert "Петренко" not in text and "Клієнт" not in text, "ні назв файлів, ні тек, ні клієнтів"
