"""Геометрія файлу диска Sum3D (`sum3d_blank_shape`) — на синтетичному файлі.

Справжнього `.blk` у розробці немає (він лише на ПК цеху), тому файл тут
складено за тим, що показав прод: заголовок `<SUM3D_BLANK V1.2>`, трійки
`double` (x, y, висота) з кроком 24 байти на довільному зсуві, між ними
цілі числа й трохи тексту.
"""

from __future__ import annotations

import json
import math
import struct

from app.services import sum3d_blank_shape as shape
from app.settings_store import set_setting
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура
from tests.test_sum3d_files import _call_tool


def _circle(cx: float, cy: float, radius: float, z: float, points: int, *, closed: bool = True) -> bytes:
    out = b""
    last = points if closed else points - 4
    for i in range(last + (1 if closed else 0)):
        angle = 2 * math.pi * (i % points) / points
        out += struct.pack("<3d", cx + radius * math.cos(angle), cy + radius * math.sin(angle), z)
    return out


def _blank(*contours: bytes) -> bytes:
    """Заголовок непарної довжини — ряди чисел стоять НЕ на межі 8 байтів."""
    data = b"<SUM3D_BLANK V1.2>\x00" + b"Zirconium\x00" + "Петренко".encode("cp1251") + b"\x00"
    data += "Ковальчук".encode("utf-16-le") + b"\x00\x00"
    for index, contour in enumerate(contours):
        data += struct.pack("<4i", 7, index, 0, len(contour) // 24)
        data += contour
    return data + struct.pack("<2i", -1, -1)


def test_closed_contour_is_found_on_odd_offset_with_real_area():
    data = _blank(_circle(10.0, -5.0, 4.0, 9.0, 60))
    out = shape.shape_of(data)
    lines = [r for r in out["ряди"] if r.get("стала")]
    assert len(lines) == 1
    line = lines[0]
    assert line["зсув"] % 8, "ряд мусить знайтись і не на межі 8 байтів"
    assert line["стала"]["значення"] == 9.0
    assert line["точок"] == 61 and line["замкнений"] is True
    # Перша точка повторена наприкінці, тож середнє зсунуте на 4/61 мм.
    assert line["центр"] == [10.07, -5.0]
    assert abs(line["площа_мм2"] - math.pi * 16) < 0.5
    assert line["x"] == [6.0, 14.0] and line["y"][0] == -9.0
    assert len(line["точки"]) == shape.MAX_POINTS
    assert line["точки"][0] == line["точки"][-1] == [14.0, -5.0]
    assert out["поліліній_за_висотою"] == {"9.0": 1}


def test_contour_through_the_axes_is_not_torn_by_near_zero_values():
    """Коло з центром у нулі: на осях координата ~1e-16, а не рівно нуль."""
    out = shape.shape_of(_blank(_circle(0, 0, 3, 9.0, 40)))
    lines = [r for r in out["ряди"] if r.get("стала")]
    assert len(lines) == 1 and lines[0]["точок"] == 41 and lines[0]["замкнений"] is True


def test_two_contours_stay_separate_and_open_one_is_marked():
    data = _blank(_circle(0, 0, 3, 9.0, 40), _circle(20, 20, 5, 6.0, 40, closed=False))
    out = shape.shape_of(data)
    lines = [r for r in out["ряди"] if r.get("стала")]
    assert [row["стала"]["значення"] for row in lines] == [9.0, 6.0]
    assert [row["замкнений"] for row in lines] == [True, False]
    assert out["поліліній_за_висотою"] == {"9.0": 1, "6.0": 1}
    # Лічильник точок стоїть цілим числом одразу перед рядом — його видно.
    before_second = [g for g in out["проміжки"] if g["зсув"] + g["довжина"] == lines[1]["зсув"]][0]
    assert before_second["int32_перед_рядом"][-1] == 36


def test_text_never_leaves_the_file():
    data = _blank(_circle(0, 0, 3, 9.0, 30))
    dumped = json.dumps(shape.shape_of(data), ensure_ascii=False)
    hex_all = " ".join(
        g.get("hex", "") + g.get("hex_початок", "") + g.get("hex_кінець", "")
        for g in shape.shape_of(data)["проміжки"]
    )
    assert "Zirconium".encode().hex(" ") not in hex_all
    assert "Петренко".encode("cp1251").hex(" ") not in hex_all
    assert "Ковальчук".encode("utf-16-le").hex(" ") not in hex_all
    assert "Петренко" not in dumped and "Zirconium" not in dumped
    assert shape.shape_of(data)["затерто_відрізків_тексту"] >= 3


def test_numbers_without_constant_height_are_not_called_a_contour():
    matrix = struct.pack("<12d", 1, 0, 0, 12.5, 0, 1, 0, -3.25, 0, 0, 1, 7.75)
    out = shape.shape_of(b"HEAD\x01\x02\x03" + matrix + b"\xff" * 9)
    assert len(out["ряди"]) == 1
    run = out["ряди"][0]
    assert "стала" not in run and "точки" not in run
    assert run["чисел"] == 12 and len(run["межі_крок3"]) == 3


def test_zero_padding_and_garbage_give_no_runs():
    out = shape.shape_of(b"\x00" * 4096 + bytes(range(256)) * 8)
    assert out["ряди"] == []
    assert out["поліліній_за_висотою"] == {}


def test_oversized_file_is_refused_not_scanned():
    out = shape.shape_of(b"\x00" * (shape.MAX_SHAPE_BYTES + 1))
    assert "завеликий" in out["помилка"]


def test_missing_file_is_a_word_not_an_exception(tmp_path):
    assert "не прочитано" in shape.describe_shape(str(tmp_path / "нема.blk"))["помилка"]


def test_tool_blk_shape_reads_one_newest_disc(app_db, tmp_path):  # noqa: F811
    app, session_factory = app_db
    folder = tmp_path / "blanks" / "Zr" / "18"
    folder.mkdir(parents=True)
    (folder / "zr18_18-Monolith-A1-x22.blk").write_bytes(_blank(_circle(1, 2, 3, 9.0, 24)))
    with session_factory() as db:
        set_setting(db, "cam_blanks_path", str(tmp_path / "blanks"))
        db.commit()
    data = _call_tool(app, {"kind": "blk_shape", "brand": "x22"})["structuredContent"]
    assert [d["файл"] for d in data["диски"]] == ["zr18_18-Monolith-A1-x22.blk"]
    assert data["диски"][0]["поліліній_за_висотою"] == {"9.0": 1}


def test_tool_blk_shape_without_folder_says_where_to_set(app_db):  # noqa: F811
    app, _ = app_db
    result = _call_tool(app, {"kind": "blk_shape"})
    assert result["isError"] is True
    assert "Нові диски" in json.dumps(result, ensure_ascii=False)
