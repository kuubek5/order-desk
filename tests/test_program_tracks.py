"""Слід різання програми верстата (власник 14.11.26): теки з Machines.arl,
розбір `.iso`, збір лише читанням, звірка з обрисами диска."""

from __future__ import annotations

import os
import time

from app.services import program_tracks as pt

ARL = r"""R4SUM_MACHINE 1 4
<MACHINESTART>
  Machine=350i_Boris
  ControlType=ISO
  DirPostProcess=C:\CAM-out\350i_Boris
  ExtPostProcess=.iso
<MACHINEEND>
<MACHINESTART>
  Machine=CORiTEC 250i PRO+_Tolik
  DirPostProcess=\\SN2024s2032\Users\SmartControl\Documents\SmartControl\Jobs
  ExtPostProcess=.iso
<MACHINEEND>
<MACHINESTART>
  Machine=Без теки
<MACHINEEND>
"""


def _square(cx, cy, half):
    return [(cx - half, cy - half), (cx + half, cy - half), (cx + half, cy + half), (cx - half, cy + half)]


def _program(cx, cy, *, mirror_back=True, patient="Петренко"):
    """Програма: обдирання квадрата 8×8 мм довкола (cx, cy) згори й знизу,
    плюс нахилений прохід і підхід на безпечній висоті."""
    rows = ["%1000", ";imes-icore - iCAM V5 generated file", "G17", "G90", "G0 Z65",
            ";Blank: zr16_16-Monolith-A4-x05", f";Object: 2026-11-14_{patient}-16-crown_cad.stl",
            "G0 A0.0 B0.0", f"G1 X{cx} Y{cy} Z65.0 F6000."]
    for side, a in (("top", 0.0), ("back", 180.0)):
        rows.append(f"G0 A{a} B0.0")
        y_sign = -1 if (side == "back" and mirror_back) else 1
        step = 0
        while step <= 16:
            dx = -4 + step * 0.5
            for dy in (-4, 4):
                rows.append(f"G1 X{cx + dx:.3f} Y{(cy + dy) * y_sign:.3f} Z5.0")
            for d in (-4, 4):
                rows.append(f"G1 X{cx + d:.3f} Y{(cy + dx) * y_sign:.3f}")
            step += 1
    rows += ["G0 A0.0 B12.5", "G1 X40.0 Y40.0 Z3.0", "G0 A0 B0", "M5", "M30"]
    return ("\r\n".join(rows) + "\r\n").encode("ascii", "replace")


def test_machines_arl_gives_output_folders():
    found = pt.parse_machines(ARL)
    bs = chr(92)
    assert [(m.machine, m.path, m.is_network) for m in found] == [
        ("350i_Boris", bs.join(["C:", "CAM-out", "350i_Boris"]), False),
        ("CORiTEC 250i PRO+_Tolik",
         bs * 2 + bs.join(["SN2024s2032", "Users", "SmartControl", "Documents", "SmartControl", "Jobs"]), True),
    ]


def test_program_gives_cells_without_tilted_and_safe_height_moves():
    track = pt.parse_program(_program(10, 20), "2_16-Monolith-A4-x05_2026-11-14_16-07-15.iso")
    assert set(track.sides) == {"0", "180"}
    xs = [c[0] * pt.CELL_MM for c in track.sides["0"]]
    ys = [c[1] * pt.CELL_MM for c in track.sides["0"]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (6.0, 14.0, 16.0, 24.0)
    assert track.skipped_tilted == 1, "нахилений прохід у площину диска не лягає"
    assert track.blank == "zr16_16-Monolith-A4-x05" and track.objects == 1


def test_track_keeps_no_object_names(tmp_path):
    track = pt.parse_program(_program(10, 20), "p_2026-11-14_16-07-15.iso")
    saved = pt.save_track(track, tmp_path).read_text(encoding="utf-8")
    assert "Петренко" not in saved and "crown_cad" not in saved


def test_match_finds_own_contour_and_picks_mirror():
    contours = [_square(10, 20, 4.5), _square(-20, -10, 5), _square(25, -25, 5)]
    track = pt.parse_program(_program(10, 20, mirror_back=True), "p.iso")
    report = pt.match(track, 47.0, contours)
    assert report["свої_обриси"] == [1] and report["надійно"]
    assert report["боки"]["0"]["знак_xy"] == [1, 1]
    assert report["боки"]["180"]["знак_xy"] == [1, -1], "задній бік дзеркалиться — підбирається сам"


def test_track_off_the_contours_is_not_trusted():
    contours = [_square(-20, -10, 5), _square(25, -25, 5)]
    report = pt.match(pt.parse_program(_program(10, 20), "p.iso"), 47.0, contours)
    assert report["свої_обриси"] == [] and not report["надійно"]


def test_collect_reads_new_finished_programs_only(tmp_path):
    src, cache = tmp_path / "CAM-out", tmp_path / "cache"
    src.mkdir()
    name = "2_16-Monolith-A4-x05_2026-11-14_16-07-15.iso"
    (src / name).write_bytes(_program(10, 20))
    (src / "half_2026-11-14_16-09-00.iso").write_bytes(_program(0, 0).replace(b"M30", b""))  # ще пишеться
    (src / "note.txt").write_text("не програма", encoding="utf-8")
    old = time.time() - 60
    for f in src.iterdir():
        os.utime(f, (old, old))
    before = {p.name: p.read_bytes() for p in src.iterdir()}
    assert pt.collect_folder(str(src), target=cache) == 1
    assert pt.collect_folder(str(src), target=cache) == 0, "удруге той самий файл не читається"
    assert {p.name: p.read_bytes() for p in src.iterdir()} == before, "тека верстата не змінюється"
    assert not list(src.glob("*.part")) and not list(src.glob("*.json"))
    # Верстат забрав файл — слід лишився в кеші KuubMill.
    (src / name).unlink()
    found = pt.find_track(name, cache)
    assert found is not None and found.cells > 0 and found.source == str(src)
    # Екран нового верстата показує лише хвіст назви — знаходимо за «дата_час».
    assert pt.find_track("2026-11-14_16-07-15.iso", cache) is not None
    assert pt.find_track("2026-11-14_10-00-00.iso", cache) is None


def test_missing_folder_is_remembered_and_not_fatal(tmp_path):
    assert pt.collect_folder(str(tmp_path / "нема"), target=tmp_path / "cache") == 0
    state = pt.folder_status()[str(tmp_path / "нема")]
    assert state["помилка"] == "FileNotFoundError"


def test_prune_drops_old_tracks(tmp_path):
    track = pt.parse_program(_program(1, 1), "old_2026-10-01_10-00-00.iso")
    path = pt.save_track(track, tmp_path)
    long_ago = time.time() - (pt.KEEP_DAYS + 1) * 86400
    os.utime(path, (long_ago, long_ago))
    assert pt.prune(tmp_path) == 1 and not path.exists()


def test_iso_link_tool_reports_folders_from_sum3d(tmp_path, monkeypatch):
    """MCP `kind=iso_link` на порожній базі: теки з Machines.arl видно, верстатів
    немає — без помилки. Лише читання."""

    from app.services import sum3d_install
    from app.services.mcp_tools import TOOLS_BY_NAME

    install = tmp_path / "sum3d"
    out = tmp_path / "CAM-out"
    install.mkdir()
    out.mkdir()
    (install / "Machines.arl").write_text(
        "<MACHINESTART>\n  Machine=350i_Boris\n  DirPostProcess=" + str(out) + "\n<MACHINEEND>\n",
        encoding="latin-1")
    monkeypatch.setattr(sum3d_install, "read_installs",
                        lambda db: [sum3d_install.Install("Sum3D", str(install))])
    monkeypatch.setattr(pt, "_folders_cache", (0.0, []))
    monkeypatch.setattr(pt, "cache_dir", lambda: tmp_path / "cache")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401
    from app.db import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        report = TOOLS_BY_NAME["kmill_sum3d_files"].run(db, {"kind": "iso_link"})
    assert report["теки_програм"][0]["тека"] == str(out)
    assert report["теки_програм"][0]["верстати_sum3d"] == ["350i_Boris"]
    assert report["верстати"] == [] and report["знято_всього"] == 0


def test_watcher_keeps_program_that_machine_took_away(tmp_path):
    """Власник 14.11.26: старий верстат забирає `.iso` з теки. Сторож відкриває
    файл одразу (ще недописаний) і дочитує його вже ПІСЛЯ того, як файл зник."""
    src, cache = tmp_path / "CAM-out", tmp_path / "cache"
    src.mkdir()
    name = "2_16-Monolith-A4-x05_2026-11-14_16-07-15.iso"
    full = src / name
    body = _program(10, 20)
    try:
        with open(full, "wb") as writer:               # Sum3D ще пише
            writer.write(body[: len(body) // 2])
            writer.flush()
            assert pt.watch_folder(str(src), target=cache) == 0
            assert pt.folder_status()[str(src)]["тримаємо_відкритими"] == 1
            writer.write(body[len(body) // 2:])        # дописав — наш дозвіл на запис не заважає
        os.remove(full)                                # верстат забрав до наступного перегляду
        assert not full.exists()
        assert pt.watch_folder(str(src), target=cache) == 1
    finally:
        pt.release_all()
    track = pt.find_track(name, cache)
    assert track is not None and track.file_size == len(body) and set(track.sides) == {"0", "180"}
    state = pt.folder_status()[str(src)]
    assert state["останній_забраний"] == name and state["тримаємо_відкритими"] == 0
    assert list(src.iterdir()) == [], "у теці верстата нічого не лишилось і не з'явилось"


def test_watcher_takes_finished_program_and_lets_it_be_moved(tmp_path):
    src, cache, away = tmp_path / "CAM-out", tmp_path / "cache", tmp_path / "machine"
    src.mkdir()
    away.mkdir()
    name = "5pmma16_16-a3-x137_2026-11-14_15-48-58.iso"
    (src / name).write_bytes(_program(-15, 5))
    try:
        assert pt.watch_folder(str(src), target=cache) == 1, "дописана програма знімається з першого перегляду"
        assert pt.watch_folder(str(src), target=cache) == 0
        os.replace(src / name, away / name)            # верстат переносить — ми не тримаємо
    finally:
        pt.release_all()
    assert (away / name).read_bytes() == _program(-15, 5)
