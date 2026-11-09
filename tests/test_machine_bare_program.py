"""Програма без часу Sum3D у назві — на табло як є (власник 09.11.26).

Цех 09.11.26: 250i-Tolik і 250i-Sec фрезерували `DDD.ISO` (екран JOBS), а
350i-Boris — `-6-_16-Monolith-A1-x26_2026-11-09_.iso` (заголовок RemiCORE без
часу). Читачі мовчали, бо шукали `дата_час`, і табло було порожнє. Назву тепер
показуємо, але з роботою в черзі не звʼязуємо: Sum3D ID у ній немає.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.services import machines as ms
from app.services import screen_inbox
from app.services.shop_board import program_line

FIX = Path(__file__).parent / "fixtures"
BORIS_TITLE = "Remote - -6-_16-Monolith-A1-x26_2026-11-09_.iso"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)
    monkeypatch.setattr(screen_inbox, "root", lambda: tmp_path / "puzzles")
    screen_inbox.reset_state_for_tests()
    target = ms.MachineTarget(name="350i-Boris", host="10.0.0.76", port=8765, agent_token="t")
    yield target
    with ms._states_lock:
        ms._states.pop(target.key, None)
    screen_inbox.reset_state_for_tests()


def _blank():
    return Image.new("RGB", (1280, 1024), (240, 240, 240))


def test_title_without_time_shows_the_name_but_binds_nothing(agent, db_session):
    state = ms.poll_target(
        db_session, agent, password=None, frame=_blank(),
        titles=["433590827 - RustDesk", BORIS_TITLE, "Program Manager"],
    )
    assert state.iso_name == "-6-_16-Monolith-A1-x26_2026-11-09_.iso"
    assert state.sum3d_id is None
    assert state.last_sum3d_id is None
    head, tail = program_line(state.iso_name)
    assert head == "-6-_16-Monolith-A1-x26_2026-11-09_" and tail == ""


def test_name_goes_away_with_the_window(agent, db_session):
    ms.poll_target(db_session, agent, password=None, frame=_blank(), titles=[BORIS_TITLE])
    state = ms.poll_target(db_session, agent, password=None, frame=_blank(), titles=["Remote"])
    assert state.iso_name is None


def test_a_dated_title_still_wins(agent, db_session):
    state = ms.poll_target(
        db_session, agent, password=None, frame=_blank(),
        titles=["Remote - zr18_18-Monolith-A3-x62_2026-09-02_23-04-33.iso"],
    )
    assert state.sum3d_id == "23-04-33"


@pytest.mark.parametrize("frame", ["newgen_250i_ddd.png", "newgen_250i_ddd_sec.png"])
def test_jobs_screen_ddd_iso_is_shown_as_is(agent, db_session, frame):
    with Image.open(FIX / frame) as opened:
        image = opened.convert("RGB")
    program = ms._program_from_screen(db_session, agent, image)
    assert program is not None
    assert (program.iso_name, program.sum3d_id) == ("DDD.ISO", "")
    assert program_line(program.iso_name) == ("DDD", "")


def test_a_dated_jobs_screen_is_untouched(agent, db_session):
    """Строгий читач далі вирішує все, де є дата: бере ТЕ САМЕ, що й раніше."""
    with Image.open(FIX / "newgen_150i_19-19-22.png") as opened:
        image = opened.convert("RGB")
    program = ms._program_from_screen(db_session, agent, image)
    # Робота від 06.11 не в черзі й не свіжа — як і раніше, нічого.
    assert program is None
