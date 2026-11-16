"""Сторож розкладки іконок Sum3D (`sum3d_layout`, 16.11.26). Лише читання CAMWINC.INI."""

from __future__ import annotations

import os

import pytest

from app.services import sum3d_layout as sl

INI = """[VERSION]
Number=7.010000
[CamToolbarsFlat-Bar1]
BarID=199
XPos={x}
YPos=-2
Docking=1
[CamToolbarsFlat-Bar2]
BarID=131
Visible=0
[CamToolbarsFlat-Bar3]
BarID=465
XPos=185
YPos=-2
Docking=1
[CamToolbarsFlat-Bar39]
BarID=59419
Bars=5
Bar#0=0
Bar#1=465
Bar#2=199
Bar#3=0
Bar#4=131
[CamToolbarsFlat-Summary]
Bars=4
ScreenCX={cx}
ScreenCY=1440
[LOCKING]
USERNAME=1-PC
DATE/TIME={stamp}
[Recent File List]
File1=D:\\CAM-work\\2026-11-14_11-07-11.cam
"""


def _ini(x=1021, cx=2560, stamp="16:13:41 16-11-2026"):
    return INI.format(x=x, cx=cx, stamp=stamp)


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    sl.reset_for_tests()
    monkeypatch.setattr(sl, "state_path", lambda: tmp_path / "state.json")
    procs: dict[int, tuple[str, float]] = {}
    monkeypatch.setattr(sl, "_running", lambda: dict(procs))
    folder = tmp_path / "iCAMV5smart"
    folder.mkdir()
    yield folder, procs
    sl.reset_for_tests()


def _write(folder, text, tick):
    path = folder / sl.INI_NAME
    path.write_text(text, encoding="cp1252")
    os.utime(path, (tick, tick))


def test_parse_reads_bars_docks_and_screen():
    layout = sl.parse_layout(_ini())
    assert layout["bars"]["199"] == {"visible": True, "x": 1021, "y": -2, "floating": False}
    assert layout["bars"]["131"]["visible"] is False
    assert layout["docks"]["59419"] == [["465", "199"], ["131"]]
    assert layout["screen"] == [2560, 1440]
    # Недавні файли й блокування в розкладку не потрапляють.
    assert "CAM-work" not in str(layout)


def test_signature_ignores_locking_but_sees_moved_bar():
    base = sl.signature(sl.parse_layout(_ini()))
    assert sl.signature(sl.parse_layout(_ini(stamp="09:00:00 17-11-2026"))) == base
    assert sl.signature(sl.parse_layout(_ini(x=40))) != base


def test_watch_records_change_after_a_close(watcher):
    folder, procs = watcher
    _write(folder, _ini(), 1_000)
    procs[11] = (r"C:\iCAMV5smart\camwinc.exe", 1.0)
    procs[22] = (r"D:\SUM\CAMWINC.EXE", 2.0)
    assert sl.watch_once([str(folder)], now=100.0) == 1          # перший знімок, процеси лише запам'ятано
    # Sum3D переписала файл при старті (блокування) — розкладка та сама, подій немає.
    _write(folder, _ini(stamp="09:00:00 17-11-2026"), 1_010)
    assert sl.watch_once([str(folder)], now=105.0) == 0
    # Друга Sum3D закрилась і записала свою розкладку при іншій роздільності.
    del procs[22]
    _write(folder, _ini(x=40, cx=1920), 1_020)
    assert sl.watch_once([str(folder)], now=110.0) == 2
    report = sl.report()
    change, close = report["події"][0], report["події"][1]
    assert close["подія"] == "закриття" and close["exe"] == r"D:\SUM\CAMWINC.EXE"
    assert change["подія"] == "розкладка_змінилась" and change["екран"] == [1920, 1440]
    assert change["щойно_закрито"] == [r"D:\SUM\CAMWINC.EXE"]
    assert change["запущені"] == [r"C:\iCAMV5smart\camwinc.exe"]
    assert change["змінено"] == [{"панель": "199", "було": "у доку x=1021 y=-2", "стало": "у доку x=40 y=-2"}]
    layouts = report["файли"][0]["розкладки"]
    assert len(layouts) == 2
    assert report["файли"][0]["зараз"] == sl.signature(sl.parse_layout(_ini(x=40, cx=1920)))
    assert "layout" not in layouts[0]


def test_restart_of_kuubmill_does_not_invent_launches(watcher):
    folder, procs = watcher
    procs[11] = (r"C:\iCAMV5smart\camwinc.exe", 1.0)
    sl.watch_once([str(folder)], now=1.0)
    sl.reset_for_tests()  # як рестарт: пам'ять процесу порожня, історія на диску лишилась
    assert sl.watch_once([str(folder)], now=2.0) == 0
    assert all(e["подія"] != "запуск" for e in sl.report()["події"])


def test_missing_install_folder_is_quiet(watcher, tmp_path):
    assert sl.watch_once([str(tmp_path / "нема")], now=1.0) == 0
