"""Справжній диск на плитці (`sum3d_disc`) і повна назва програми CORiTEC
(`sum3d_names`). Файли — синтетичні, у форматі, розібраному на проді 12.11.26
(диск x54: `<9560>` = 8 + 6·4 + 397·24)."""

from __future__ import annotations

import math
import struct

from app.services import sum3d_disc, sum3d_names


def _square(cx, cy, half, n=12):
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        pts.append((cx + half * math.cos(a), cy + half * math.sin(a)))
    return pts


def _blank(contours, radius=47.0, tail=b""):
    total = sum(len(c) for c in contours)
    block = struct.pack("<ii", len(contours), total)
    for c in contours:
        block += struct.pack("<i", len(c))
        for x, y in c:
            block += struct.pack("<3d", x, y, 0.0)
    circle = struct.pack("<i", 64)
    for i in range(64):
        a = 2 * math.pi * i / 64
        circle += struct.pack("<3d", radius * math.cos(a), radius * math.sin(a), -10.0)
    head = b"<SUM3D_BLANK V1.2>\r\n<MM>\r\n<0>\r\n<20-?>\r\n"
    return (head + b"<%d>\r\n" % len(block) + block + b"<0>\r\n<%d>\r\n" % len(circle)
            + circle + tail)


def test_parses_contours_radius_and_free_share():
    shape = sum3d_disc.parse_blank(_blank([_square(10, 5, 6), _square(-20, -10, 8, 20)]))
    assert shape is not None and len(shape.contours) == 2 and abs(shape.radius - 47) < 0.01
    used = sum(shape.areas)
    assert abs(shape.free_share - (1 - used / (math.pi * 47 ** 2))) < 1e-9
    paths = sum3d_disc.svg_paths(shape)
    assert len(paths) == 2 and paths[0].startswith("M") and paths[0].endswith("Z")
    # y угору, як у Sum3D: точка (10+6, 5) → x>50, y<50 у координатах SVG.
    first = paths[0][1:].split("L")[0].split()
    assert float(first[0]) > 50 and float(first[1]) < 50


def test_empty_disc_and_foreign_bytes():
    shape = sum3d_disc.parse_blank(_blank([]))
    assert shape is not None and shape.contours == [] and shape.free_share == 1.0
    assert sum3d_disc.parse_blank(b"not a blank") is None
    assert sum3d_disc.parse_blank(_blank([_square(0, 0, 5)])[:60]) is None,         "обрізаний файл — «диска немає», а не виняток"


def test_disc_key_follows_the_part_after_the_first_underscore():
    assert sum3d_disc.disc_key("4pmma25_25-a2-x276_2026-11-12_01-27-41.iso") == "25-a2-x276"
    assert sum3d_disc.disc_key("2_16-Emotions-a3-5-x27_2026-11-12_00-05-32.iso") == "16-emotions-a3-5-x27"
    assert sum3d_disc.disc_key("pmma18_18-a3-5-x41_2026-11-12_00-47-30.iso") == "18-a3-5-x41"
    assert sum3d_disc.disc_key("2026-11-12_01-20-12.iso") is None, "гола дата — диска не знаємо"
    assert sum3d_disc.disc_key(None) is None


def test_finds_the_blank_file_and_tells_pmma_from_zirconia(tmp_path):
    for rel in ("zr/16/zr16_16-Emotions-a3-5-x27.blk", "pmma/25/pmma25_25-a2-x276.blk",
                "zr/25/zr25_25-a2-x276.blk", "zr/16/zr16_16-Monolith-A3-x104.blk"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(_blank([_square(0, 0, 5)]))
    root = str(tmp_path)
    sum3d_disc._list_cache.clear()
    assert sum3d_disc.find_blank(root, "2_16-Emotions-a3-5-x27_2026-11-12_00-05-32.iso").name == \
        "zr16_16-Emotions-a3-5-x27.blk"
    assert sum3d_disc.find_blank(root, "4pmma25_25-a2-x276_2026-11-12_01-27-41.iso").name == \
        "pmma25_25-a2-x276.blk", "той самий хвіст у цирконію — вирішує «pmma» в початку назви"
    assert sum3d_disc.find_blank(root, "1_25-a2-x276_2026-11-12_01-27-41.iso").name == "zr25_25-a2-x276.blk"
    assert sum3d_disc.find_blank(root, "1_16-Monolith-A3-x999_2026-11-12_01-27-41.iso") is None


def test_tile_gets_the_real_disc(tmp_path, monkeypatch):
    from tests.test_shop_board import _card, _view

    f = tmp_path / "pmma" / "18" / "pmma18_18-a3-5-x41.blk"
    f.parent.mkdir(parents=True)
    f.write_bytes(_blank([_square(10, 5, 6), _square(-20, -10, 8)]))
    sum3d_disc._list_cache.clear()
    monkeypatch.setattr(sum3d_disc, "_root", str(tmp_path))
    card = _card("A", "run", percent=40, iso_name="pmma18_18-a3-5-x41_2026-11-12_00-47-30.iso")
    machine = _view([card]).machines[0]
    assert machine.disc is not None and len(machine.disc.paths) == 2 and 90 < machine.disc.free_pct < 100
    from app.routers.deps import templates

    html = templates.env.get_template("_shop_board_cards.html").render(view=_view([card]), token="t")
    assert html.count('<path class="rc"') == 2 and "вільно ≈" in html and 'class="crown' not in html
    idle = _view([_card("B", "idle", iso_name="pmma18_18-a3-5-x41_2026-11-12_00-47-30.iso")]).machines[0]
    assert idle.disc is None, "без відсотка диск не шукаємо"


def test_full_program_name_comes_from_the_sum3d_project(tmp_path):
    cam = tmp_path / "2026-11-12_01-20-12.cam"
    path_text = b"D:\\CAM-work\\2026-11-12_01-20-12.cam"
    name = "pmma16_14-a4-x81_2026-11-12_01-20-12.iso"
    cam.write_bytes(b"TECN 2.0\0" + path_text + b"\0" * 100 + name.encode("utf-16-le") + b"\0\0")
    assert sum3d_names.find_in_project(cam, "2026-11-12", "01-20-12") == name, \
        "шлях до проєкту (без голови) — не назва; UTF-16 читається"
    ascii_cam = tmp_path / "a.cam"
    ascii_cam.write_bytes(b"xx\x01" + b"2_16-Emotions-a3-5-x27_2026-11-12_01-20-12.iso" + b"\x00")
    assert sum3d_names.find_in_project(ascii_cam, "2026-11-12", "01-20-12") == \
        "2_16-Emotions-a3-5-x27_2026-11-12_01-20-12.iso"
    (tmp_path / "none.cam").write_bytes(path_text)
    assert sum3d_names.find_in_project(tmp_path / "none.cam", "2026-11-12", "01-20-12") is None
    assert sum3d_names.bare_parts("2026-11-12_01-20-12.iso") == ("2026-11-12", "01-20-12")
    assert sum3d_names.bare_parts("pmma16_14-a4-x81_2026-11-12_01-20-12.iso") is None


def test_board_shows_the_full_name_once_found(tmp_path, monkeypatch):
    from app.services import shop_board as sb  # noqa: F811
    from tests.test_shop_board import _card, _view

    monkeypatch.setattr(sum3d_names, "_root", str(tmp_path))
    monkeypatch.setattr(sum3d_names, "_found", {})
    (tmp_path / "2026-11-12_01-20-12.cam").write_bytes(b"\x01pmma16_14-a4-x81_2026-11-12_01-20-12.iso\x00")
    card = _card("A", "run", percent=10, iso_name="2026-11-12_01-20-12.iso")
    sum3d_names._search_into_cache("2026-11-12_01-20-12", ("2026-11-12", "01-20-12"), str(tmp_path))
    machine = _view([card]).machines[0]
    assert (machine.prog_head, machine.prog_tail) == ("pmma16_14-a4-x81_2026-11-12_", "01-20-12")
    assert sb.program_line("2026-11-12_01-20-12.iso") == ("2026-11-12_", "01-20-12")
