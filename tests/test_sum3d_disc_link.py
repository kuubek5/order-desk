"""Розвідка «чий обрис на диску» (`sum3d_disc_link`, kind=disc_link)."""

import struct
from types import SimpleNamespace

from app.services import mcp_tools_sum3d, sum3d_disc, sum3d_disc_link
from tests.test_sum3d_disc import _blank, _square


def _shape(*contours):
    shape = sum3d_disc.parse_blank(_blank(list(contours)))
    assert shape is not None
    return shape


def _matrix(dx, dy, dz, *, row_major=True):
    if row_major:
        values = (1, 0, 0, dx, 0, 1, 0, dy, 0, 0, 1, dz, 0, 0, 0, 1)
    else:
        values = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, dx, dy, dz, 1)
    return struct.pack("<16d", *values)


def test_points_of_one_contour_found_as_doubles_and_floats():
    own, other = _square(10.3, 5.7, 6), _square(-20.1, -10.9, 8, 20)
    shape = _shape(own, other)
    project = b"\x07" * 3 + b"".join(struct.pack("<3d", x, y, 0.5) for x, y in own)
    project += b"\x01" + b"".join(struct.pack("<2f", x, y) for x, y in own)
    report = sum3d_disc_link.link_report(project, shape)
    first, second = report["обриси"]
    assert first["точок_double"] == first["точок_перевірено"] > 0
    assert first["точок_float"] == first["точок_перевірено"]
    assert second["точок_double"] == second["точок_float"] == 0
    assert report["обриси_зі_слідом"] == [1]


def test_matrix_offset_inside_a_contour_marks_it():
    shape = _shape(_square(10.3, 5.7, 6), _square(-20.1, -10.9, 8, 20))
    project = b"\x00\x02" + _matrix(-20.0, -11.0, 3.0) + b"\x09" * 5 + _matrix(30.0, 30.0, 0, row_major=False)
    report = sum3d_disc_link.link_report(project, shape)
    assert [r["матриць_усередині"] for r in report["обриси"]] == [0, 1]
    assert report["матриць_знайдено"] == 2
    assert report["матриць_на_диску_поза_обрисами"] == 1
    assert report["обриси_зі_слідом"] == [2]


def test_identity_and_round_points_are_not_a_trace():
    shape = _shape([(1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0)])
    project = _matrix(0, 0, 0) + b"".join(struct.pack("<2d", x, y) for x, y in shape.contours[0])
    report = sum3d_disc_link.link_report(project, shape)
    assert report["обриси"][0]["точок_перевірено"] == 0
    assert report["матриць_знайдено"] == 0 and report["обриси_зі_слідом"] == []


def test_mcp_disc_link_reads_the_program_project(tmp_path, monkeypatch, db_session):
    from app.services import machines

    own = _square(10.3, 5.7, 6)
    blank = tmp_path / "zr16_16-monolith-a2-x61.blk"
    blank.write_bytes(_blank([own, _square(-20.1, -10.9, 8, 20)]))
    (tmp_path / "2026-11-13_16-29-49.cam").write_bytes(
        b"".join(struct.pack("<3d", x, y, 0.0) for x, y in own))
    program = "2_16-Monolith-A2-x61_2026-11-13_16-29-49.iso"
    card = SimpleNamespace(is_sisma_machine=False, target=SimpleNamespace(name="350i-Boris"), iso_name=program)
    monkeypatch.setattr(machines, "snapshot", lambda db: [card])
    monkeypatch.setattr(mcp_tools_sum3d, "_machine_disc",
                        lambda c, b, p: (program, program, "екран/заголовок", blank, "з назви програми"))
    result = mcp_tools_sum3d._sum3d_disc_link(db_session, str(tmp_path), str(tmp_path), {})
    row = result["верстати"][0]
    assert row["обриси_зі_слідом"] == [1] and row["одиниць_у_черзі"] == {"робіт": 0, "одиниць": 0}
    assert "_image" in result
