"""Звірка підказки диска з тим, що поставив Sum3D (`disc_fit_replay`, 16.11.26)."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np

from app.services import disc_fit_replay as rp
from app.services import disc_suggest, program_tracks, sum3d_disc
from tests.test_program_tracks import _program
from tests.test_sum3d_disc import _blank


def _ring(cx, cy, r, n=40):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _crown(r=3.0, n=200):
    """Точки силуету коронки радіуса r (мм) — заповнений круг."""
    pts = [(x, y) for x in np.arange(-r, r + 0.01, 0.25) for y in np.arange(-r, r + 0.01, 0.25)
           if x * x + y * y <= r * r]
    return np.array(pts)


def test_polygon_area_and_margin_search():
    assert abs(rp._polygon_area([(0, 0), (10, 0), (10, 5), (0, 5)]) - 50) < 1e-9
    areas = rp._silhouette_areas([_crown(3.0)], 1.0)
    assert areas[0.0] < areas[2.0] < areas[4.0], "більший запас — більша площа"
    # Круг r=3 з запасом 2 мм ≈ круг r=5: площа ≈ 78.5 мм².
    assert abs(areas[2.0] - math.pi * 25) / (math.pi * 25) < 0.15


def test_place_compares_our_silhouette_with_the_sum3d_contour(tmp_path, monkeypatch):
    blanks, tracks = tmp_path / "b", tmp_path / "t"
    disc = blanks / "zr" / "16" / "zr16_16-Monolith-A4-x05.blk"
    disc.parent.mkdir(parents=True)
    # Обрис Sum3D навколо роботи (10, 20) — коло r=4,8; два чужі обриси.
    disc.write_bytes(_blank([_ring(10, 20, 4.8), _ring(-20, -10, 6), _ring(25, -25, 6)]))
    sum3d_disc._list_cache.clear()
    monkeypatch.setattr(program_tracks, "cache_dir", lambda: tracks)
    name = "2_16-Monolith-A4-x05_2026-11-16_13-03-34.iso"
    program_tracks.save_track(program_tracks.parse_program(_program(10, 20), name), tracks)
    order = SimpleNamespace(id=77, source_email_id=5, material_color="mono a4")
    monkeypatch.setattr(rp, "_orders_of", lambda db, program: [order])
    monkeypatch.setattr(disc_suggest, "letter_pieces", lambda atts: [_crown(3.0)])
    db = SimpleNamespace(get=lambda model, key: SimpleNamespace(attachments=[]))
    out = rp.place(db, str(blanks))
    assert out["звірено"] == 1 and out["наше_влазить"] == 1
    row = out["приклади"][0]
    assert row["роботи"] == [77] and row["обрисів_sum3d"] == 1
    # Коронка r=3 × усадка 1.2 = r≈3.6; обрис r=4.8 → запас ≈ 1.2 мм.
    assert 0.5 <= row["запас_як_у_sum3d_мм"] <= 1.5
    assert "запас_sum3d_мм" in out and out["запас_sum3d_мм"]["у_нас_зараз"] == 2.8


def test_place_skips_programs_it_cannot_judge(tmp_path, monkeypatch):
    tracks = tmp_path / "t"
    monkeypatch.setattr(program_tracks, "cache_dir", lambda: tracks)
    program_tracks.save_track(program_tracks.parse_program(_program(10, 20), "1_16-X-x01_2026-11-16_10-00-00.iso"),
                              tracks)
    sum3d_disc._list_cache.clear()
    out = rp.place(SimpleNamespace(), str(tmp_path / "none"))
    assert out.get("звірено") is None and out["диска_вже_немає_або_порожній"] == 1


def _crowded_disc(tmp_path, monkeypatch, other_hour):
    """Диск r=20: обрис роботи (0, 0) і чотири обриси інших програм довкола.
    Програма роботи о 12:00, інші — о `other_hour`."""
    blanks, tracks = tmp_path / "b", tmp_path / "t"
    disc = blanks / "zr" / "16" / "zr16_16-Monolith-A4-x05.blk"
    disc.parent.mkdir(parents=True)
    others = [(10, 0), (-10, 0), (0, 10), (0, -10)]
    disc.write_bytes(_blank([_ring(0, 0, 4.8)] + [_ring(x, y, 5) for x, y in others], radius=20.0))
    sum3d_disc._list_cache.clear()
    monkeypatch.setattr(program_tracks, "cache_dir", lambda: tracks)
    own = "2_16-Monolith-A4-x05_2026-11-16_12-00-00.iso"
    program_tracks.save_track(program_tracks.parse_program(_program(0, 0), own), tracks)
    # Та сама програма під другою назвою (`+_…`, той самий час) — дубль.
    program_tracks.save_track(program_tracks.parse_program(_program(0, 0), "+" + own[1:]), tracks)
    for i, (x, y) in enumerate(others):
        name = f"{i + 3}_16-Monolith-A4-x05_2026-11-16_{other_hour}-0{i}-00.iso"
        program_tracks.save_track(program_tracks.parse_program(_program(x, y), name), tracks)
    order = SimpleNamespace(id=77, source_email_id=5, material_color="mono a4")
    monkeypatch.setattr(rp, "_orders_of", lambda db, program: [order] if "12-00-00" in program else [])
    monkeypatch.setattr(disc_suggest, "letter_pieces", lambda atts: [_crown(3.0)])
    db = SimpleNamespace(get=lambda model, key: SimpleNamespace(attachments=[]))
    return rp.place(db, str(blanks))


def test_place_removes_contours_of_later_programs_on_the_same_disc(tmp_path, monkeypatch):
    out = _crowded_disc(tmp_path, monkeypatch, other_hour="13")
    assert out["звірено"] == 1 and out["дубль_сліду"] == 1
    assert out["наше_влазить"] == 1 and out["не_влазило_лише_через_пізніші"] == 1
    row = out["приклади"][0]
    assert row["прибрано_пізніших_обрисів"] == 4 and row["невідомих_обрисів_на_диску"] == 0
    assert row["наше_влазить"] is True and row["наше_влазило_б_з_пізнішими"] is False


def test_place_keeps_contours_of_earlier_programs(tmp_path, monkeypatch):
    out = _crowded_disc(tmp_path, monkeypatch, other_hour="11")
    assert out["звірено"] == 1 and out["наше_не_влазить"] == 1
    row = out["приклади"][0]
    assert row["прибрано_пізніших_обрисів"] == 0 and row["наше_влазить"] is False
