"""Повна назва програми нового покоління (власник 23.11.26: «один раз налаштувати»).

Що стережеться:
* живі кадри 250i-Tolik і 150i-Olejka, де посимвольне правило відмовляло, читаються
  за переліком дисків — точно, серед щільних сусідів;
* немає справжнього диска в переліку — назви немає, сусіда не підставляємо;
* жоден із бойових рядків не дає хибної назви;
* файл програми (слід) важливіший за екран; Sum3D ID й дата не змінюються ніколи;
* мережеві теки перевіряються кожна своїм потоком і не дублюються.
"""

import re
import threading
from pathlib import Path

import pytest
from PIL import Image

from app import machine_newgen_job as ng
from app.machine_ocr import MillingProgram
from app.services import machine_disc_names as mdn
from app.services import program_tracks
from tests import test_machine_newgen_job as newgen

FIX = Path(__file__).parent / "fixtures"
TOLIK = "newgen_250i_15-20-09.png"
OLEJKA = "newgen_150i_17-25-10.png"
LIVE = {
    TOLIK: ("-26-ZR18_18-EMOTIONS-A1-X198", "zr18_18-Emotions-a1-x198", "2026-11-23", "15-20-09"),
    OLEJKA: ("12ZR20_20-MONOLITH-B1-X10", "zr20_20-Monolith-B1-x10", "2026-11-23", "17-25-10"),
}


@pytest.fixture(autouse=True)
def _clean():
    mdn.reset_for_tests()
    program_tracks._names.clear()
    yield
    mdn.reset_for_tests()
    program_tracks._names.clear()


def _open(name):
    return Image.open(FIX / name).convert("RGB")


def _key(head):
    tail = head.split("_")[-1]
    found = re.search(r"(?<!\d)(\d{2}-[A-Z0-9\-]*X?\d+)$", tail) or re.search(r"(\d{2}-.*)$", tail)
    return found.group(1)


def _neighbours(key):
    """Кожна цифра — будь-яка інша, кожна літера — з вивчених: найщільніші сусіди."""
    out = set()
    for i, char in enumerate(key):
        pool = "0123456789" if char.isdigit() else "ABCDEMONLITHSX" if char.isalpha() else ""
        out.update(key[:i] + other + key[i + 1:] for other in pool)
    out.discard(key)
    return [f"zr{key[:2]}_{k}" for k in out]


def _rows():
    for name, full in newgen.FULL_NAMES.items():
        yield name, full.rsplit("_", 2)[0], ng._name_layout(Image.open(FIX / name).convert("RGB"))
    for name, full in newgen.ROW_NAMES.items():
        yield name, full.rsplit("_", 2)[0], ng._layout_in_zone(Image.open(newgen.ROWS / name))
    for name, (head, *_rest) in LIVE.items():
        yield name, head, ng._name_layout(_open(name))


@pytest.mark.parametrize("name", LIVE)
def test_live_frames_the_strict_reader_refused(name):
    head, stem, day, time = LIVE[name]
    image = _open(name)
    program = ng.read_newgen_program(image)
    assert (program.date, program.sum3d_id) == (day, time)
    assert program.iso_name == f"{day}_{time}.iso", "посимвольне правило тут відмовляє — заради цього й фікс"
    dense = [stem[: stem.rindex("x") + 1] + f"{n:02d}" for n in range(1, 400)]
    assert ng.name_by_discs(image, dense + _neighbours(_key(head)))[0] == head


def test_every_shop_row_is_right_or_silent_among_dense_neighbours():
    right = 0
    for name, head, layout in _rows():
        key = _key(head)
        got, why = ng._disc_head(layout, [*_neighbours(key), f"zr{key[:2]}_{key}"])
        assert got in (head, None), f"{name}: {got!r}"
        assert got or why
        right += got == head
    assert right >= 26


def test_missing_disc_is_never_replaced_by_a_neighbour():
    """Диск ще не потрапив у перелік: назви немає, а не `15-A2-X184` замість `16-…`."""
    for name, head, layout in _rows():
        assert ng._disc_head(layout, _neighbours(_key(head)))[0] is None, name


def test_an_unlearned_symbol_still_gives_the_right_disc_or_nothing(monkeypatch):
    full = ng.load_newgen_name_glyphs()
    right = 0
    for name, head, layout in _rows():
        key = _key(head)
        stems = [*_neighbours(key), f"zr{key[:2]}_{key}"]
        for char in sorted(set(key) - {"-"}):
            monkeypatch.setattr(ng, "load_newgen_name_glyphs", lambda c=char: {k: v for k, v in full.items() if k != c})
            got = ng._disc_head(layout, stems)[0]
            assert got is None or got.endswith(key), f"{name} без «{char}»: {got!r}"
            right += got is not None
    assert right > 150


def test_unreadable_mark_leaves_the_disc_itself():
    layout = ng._name_layout(_open(TOLIK))
    original = ng._mark_text
    try:
        ng._mark_text = lambda costs, under: None
        assert ng._disc_head(layout, ["zr18_18-Emotions-a1-x198"])[0] == "ZR18_18-EMOTIONS-A1-X198"
    finally:
        ng._mark_text = original


def _bare(name=TOLIK):
    _head, _stem, day, time = LIVE[name]
    return MillingProgram(iso_name=f"{day}_{time}.iso", sum3d_id=time, date=day)


def test_file_name_wins_over_the_screen(monkeypatch):
    monkeypatch.setattr(program_tracks, "program_of",
                        lambda iso: ("-26-zr18_18-Emotions-a1-x198_2026-11-23_15-20-09.iso", "zr18_18-Emotions-a1-x198"))
    monkeypatch.setattr(mdn, "disc_stems", lambda root: pytest.fail("екран не потрібен"))
    got = mdn.complete(_bare(), "k", _open(TOLIK), "C:/discs")
    assert got.iso_name == "-26-zr18_18-Emotions-a1-x198_2026-11-23_15-20-09.iso"
    assert (got.sum3d_id, got.date) == ("15-20-09", "2026-11-23")


def test_two_tracks_with_one_tail_do_not_count_as_a_name(monkeypatch):
    """Назва зі сліду гола (два виводи) — питаємо екран, а не лишаємо дату."""
    monkeypatch.setattr(program_tracks, "program_of", lambda iso: (iso, "zr18_18-Emotions-a1-x198"))
    monkeypatch.setattr(mdn, "disc_stems", lambda root: ["zr18_18-Emotions-a1-x198"])
    got = mdn.complete(_bare(), "k", _open(TOLIK), "C:/discs")
    assert got.iso_name == "-26-ZR18_18-EMOTIONS-A1-X198_2026-11-23_15-20-09.iso"


def test_answer_is_remembered_and_a_miss_is_retried_later(monkeypatch):
    calls = []
    monkeypatch.setattr(program_tracks, "program_of", lambda iso: None)
    monkeypatch.setattr(mdn, "disc_stems", lambda root: calls.append(1) or (["zr18_18-Emotions-a1-x198"] if len(calls) > 1 else []))
    clock = [0.0]
    ask = lambda: mdn.complete(_bare(), "k", _open(TOLIK), "C:/discs", now=lambda: clock[0]).iso_name  # noqa: E731
    assert ask() == "2026-11-23_15-20-09.iso"
    assert ask() == "2026-11-23_15-20-09.iso" and len(calls) == 1, "щокадру не питаємо"
    clock[0] = mdn.MISS_TTL_SECONDS + 1
    assert ask().startswith("-26-ZR18_18") and ask().startswith("-26-ZR18_18") and len(calls) == 2


def test_full_name_and_names_without_sum3d_are_left_alone(monkeypatch):
    monkeypatch.setattr(mdn, "_resolve", lambda *a: pytest.fail("не питати"))
    for program in (MillingProgram("9_16-MONOLITH-A3-X108_2026-11-23_20-18-38.iso", "20-18-38", "2026-11-23"),
                    MillingProgram("DDD.ISO", "", "")):
        assert mdn.complete(program, "k", _open(TOLIK), "C:/discs") is program


def test_disc_list_loads_in_the_background(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.services import disc_suggest

    ready = threading.Event()
    monkeypatch.setattr(disc_suggest, "disc_files", lambda root: [SimpleNamespace(stem="zr16_16-Monolith-A2-x1")])
    original = mdn._load
    monkeypatch.setattr(mdn, "_load", lambda root: (original(root), ready.set()))
    assert mdn.disc_stems(str(tmp_path)) == [], "перший раз не чекаємо"
    assert ready.wait(5)
    assert mdn.disc_stems(str(tmp_path)) == ["zr16_16-Monolith-A2-x1"]
    assert mdn.disc_stems("") == []


def test_poller_gets_the_full_name(monkeypatch):
    from types import SimpleNamespace

    from app.services import machines

    monkeypatch.setattr(program_tracks, "program_of", lambda iso: None)
    monkeypatch.setattr(mdn, "disc_stems", lambda root: ["zr20_20-Monolith-B1-x10"])
    monkeypatch.setattr("app.settings_store.get_setting", lambda db, key: "C:/discs")
    target = SimpleNamespace(key="k", name="150i-Olejka", host="h")
    got = machines._with_full_name(None, target, _bare(OLEJKA), _open(OLEJKA))
    assert got.iso_name == "12ZR20_20-MONOLITH-B1-X10_2026-11-23_17-25-10.iso"
    monkeypatch.setattr(mdn, "complete", lambda *a, **k: 1 / 0)
    assert machines._with_full_name(None, target, _bare(OLEJKA), _open(OLEJKA)) == _bare(OLEJKA), "збій не валить опитування"


def test_network_folders_are_checked_apart_and_not_twice(monkeypatch):
    from contextlib import nullcontext

    release, seen, done = threading.Event(), [], threading.Semaphore(0)

    def collect(path):
        seen.append(path)
        if "dead" in path:
            release.wait(5)
        done.release()

    folders = [program_tracks.MachineFolder("a", r"\\dead\Jobs", ".iso"), program_tracks.MachineFolder("b", r"\\live\Jobs", ".iso"),
               program_tracks.MachineFolder("c", r"C:\CAM-out", ".iso")]
    monkeypatch.setattr(program_tracks, "known_folders", lambda db: folders)
    monkeypatch.setattr(program_tracks, "collect_folder", collect)
    monkeypatch.setattr(program_tracks, "_backoff_until", {})
    assert program_tracks.network_pass(lambda: nullcontext(None)) == 2
    assert done.acquire(timeout=5), "жива тека не чекає мертвої"
    for _ in range(100):
        if r"\\live\jobs" not in program_tracks._in_flight:
            break
        threading.Event().wait(0.01)
    assert program_tracks.network_pass(lambda: nullcontext(None)) == 1, "мертва ще в роботі — не дублюємо"
    release.set()
    assert done.acquire(timeout=5) and done.acquire(timeout=5)
    assert seen.count(r"\\dead\Jobs") == 1 and r"C:\CAM-out" not in seen
