"""Назва програми з екрана JOBS верстатів нового покоління (10.09.26).

Що ламається тихо:
- хибний Sum3D ID підсвітив би в черзі ЧУЖУ роботу як «фрезерується» — тому
  будь-який сумнів (стерта цифра, два кружки ▶, чужий екран) дає None, а не
  коротше чи «приблизне» число;
- еталони, на яких навчено, не доводять нічого про НОВІ кадри — тому окремий
  тест будує еталони лише з кадру 250i і читає ними кадр 150i, якого вони
  не бачили (інший верстат, інший розмір шрифту);
- прочитане з кадру стає прив'язкою лише коли такий Sum3D ID є в черзі.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import machine_newgen_job as ng
from app.db import Base
from app.models import Order
from app.services import machines as ms

FIX = Path(__file__).parent / "fixtures"
FRAME_250I = FIX / "newgen_progress_30.png"
FRAME_150I = FIX / "newgen_150i_38.png"


def _read(path):
    program = ng.read_newgen_program(Image.open(path))
    return (program.date, program.sum3d_id) if program else None


def test_reads_the_running_program_on_both_machines():
    assert _read(FRAME_250I) == ("2026-09-04", "12-57-22")
    assert _read(FIX / "newgen_progress_0.png") == ("2026-09-04", "12-57-22")
    assert _read(FRAME_150I) == ("2026-09-04", "14-19-00")


def test_templates_from_one_machine_read_the_other(monkeypatch):
    """Еталони лише з 250i (висота шрифту 18) читають 150i (висота 22).

    Саме на цьому тримається читання цифр, яких на верстаті ще не траплялось:
    у кадрі 150i немає ні 3, ні 5, ні 8, а в часі програми вони майже завжди є."""
    truth = "118-EMOTIONS-A1-X1932026-09-0412-57-22.ISO"
    glyphs = ng._name_glyphs(Image.open(FRAME_250I))
    assert glyphs is not None and len(glyphs) == len(truth)
    only_250i: dict[str, list] = {}
    for glyph, char in zip(glyphs, truth):
        if char.isdigit():
            only_250i.setdefault(char, []).append(ng._normalise(glyph.bits))
    monkeypatch.setattr(ng, "load_newgen_glyphs", lambda: only_250i)
    assert _read(FRAME_150I) == ("2026-09-04", "14-19-00")


def test_every_other_screen_stays_silent():
    """Жоден кадр, що не є JOBS нового покоління, не дає читання: RemiCORE,
    SUMMARY, перевірка програми, SISMA, шпалери."""
    others = [p for p in FIX.glob("*.png")
              if p.name not in {FRAME_250I.name, FRAME_150I.name, "newgen_progress_0.png"}]
    assert others, "фікстури чужих екранів зникли — тест був би порожнім"
    for path in others:
        assert _read(path) is None, path.name


def _paint(path, box, colour=(66, 66, 66)):
    image = Image.open(path).convert("RGB")
    ImageDraw.Draw(image).rectangle(box, fill=colour)
    return image


def test_a_rubbed_out_digit_gives_nothing_not_a_shorter_number():
    # Остання «2» в «12-57-22» (250i): стерли — рядок не читається зовсім.
    image = _paint(FRAME_250I, (525, 305, 545, 330))
    assert ng.read_newgen_program(image) is None


def test_a_second_play_icon_means_we_do_not_know_which_runs():
    image = Image.open(FRAME_250I).convert("RGB")
    ImageDraw.Draw(image).ellipse((129, 430, 170, 471), fill=(0, 104, 178))
    assert ng.read_newgen_program(image) is None


def test_no_templates_no_reading(monkeypatch):
    monkeypatch.setattr(ng, "load_newgen_glyphs", lambda: {})
    assert _read(FRAME_250I) is None


# ── вбудова в опитування ───────────────────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _clean_states(monkeypatch):
    monkeypatch.setattr(ms, "_states", {})
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)
    monkeypatch.setattr(ms, "_store_machine_reading", lambda *a, **k: None)


def _agent():
    return ms.MachineTarget(name="250i", host="10.0.0.81", port=8765, agent_token="t")


def test_screen_program_links_only_when_it_is_in_the_queue(db):
    frame = Image.open(FRAME_250I)
    now = datetime(2026, 9, 4, 12, 10)

    state = ms.poll_target(db, _agent(), None, now=now, frame=frame, titles=["CORiTEC 250i PRO+"])
    assert state.sum3d_id is None, "роботи немає в черзі — прив'язки бути не може"

    db.add(Order(source="lab", sheet_tab="04.09.26", row_number=3, sum3d_id="12-57-22"))
    db.commit()
    state = ms.poll_target(db, _agent(), None, now=now, frame=frame, titles=["CORiTEC 250i PRO+"])
    assert state.sum3d_id == "12-57-22"
    assert "12-57-22" in (state.iso_name or "")


@pytest.mark.parametrize("tab, archived, why", [
    ("04.08.26", False, "робота місячної давнини з тим самим часом доби"),
    ("04.09.26", True, "робота вже в архіві"),
])
def test_same_time_of_day_elsewhere_is_not_a_match(db, tab, archived, why):
    """Sum3D ID — лише час доби, і за місяці той самий `12-57-22` трапляється
    в різних роботах. Прив'язка — лише до живої роботи поруч із датою програми."""
    db.add(Order(source="lab", sheet_tab=tab, row_number=3, sum3d_id="12-57-22",
                 archived_at=datetime(2026, 9, 5) if archived else None))
    db.commit()
    state = ms.poll_target(db, _agent(), None, frame=Image.open(FRAME_250I), titles=[])
    assert state.sum3d_id is None, why


def test_title_program_wins_over_the_screen(db):
    db.add(Order(source="lab", sheet_tab="04.09.26", row_number=3, sum3d_id="12-57-22"))
    db.commit()
    title = "Remote - zr18_18-Monolith-A3-x62_2026-09-02_23-04-33.iso"
    state = ms.poll_target(db, _agent(), None, frame=Image.open(FRAME_250I), titles=[title])
    assert state.sum3d_id == "23-04-33"


def test_silent_agent_and_unreadable_screen_keep_the_last_binding(db):
    """Агент не віддав заголовки, а кадр не читається — ми НЕ знаємо, що
    фрезерується, тож стару прив'язку не знімаємо (як і було до читання з екрана)."""
    db.add(Order(source="lab", sheet_tab="04.09.26", row_number=3, sum3d_id="12-57-22"))
    db.commit()
    ms.poll_target(db, _agent(), None, frame=Image.open(FRAME_250I), titles=[])
    state = ms.poll_target(db, _agent(), None, frame=Image.open(FIX / "newgen_summary_done.png"),
                           titles=None)
    assert state.sum3d_id == "12-57-22"
    # А відповів агент без програми і екран без рядка ▶ — знімаємо.
    state = ms.poll_target(db, _agent(), None, frame=Image.open(FIX / "newgen_summary_done.png"),
                           titles=[])
    assert state.sum3d_id is None
