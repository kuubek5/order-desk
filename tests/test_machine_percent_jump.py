"""Стрибок відсотка вгору між двома кадрами — хибне читання, а не робота.

Власник 07.10.26: 250i-Tolik фрезерував 50 % (52–53 % у CRM хвилиною пізніше),
а телевізор цеху показав 100 % і танцюючого котика. Програма йде годину-дві,
тож +30 пунктів за кілька секунд не буває; такий стрибок показується, лише
коли його підтвердить наступний кадр.
"""

from datetime import datetime, timedelta

import pytest
from PIL import Image

from app.services import machines as service


@pytest.fixture
def rig(db_session, monkeypatch, tmp_path):
    service.reset_state_for_tests()
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    monkeypatch.setattr(service, "host_answers_at_all", lambda host, *a, **k: True)
    monkeypatch.setattr(service, "screen_meaning", lambda _f: None)
    target = service.MachineTarget(name="250i-Tolik", host="10.0.0.81", port=8765, agent_token="t")
    frame = Image.new("RGB", (200, 120), "black")
    readings: list = []
    monkeypatch.setattr(service, "read_progress_percent", lambda _f: readings.pop(0))
    start = datetime(2026, 10, 7, 21, 9, 0)

    def poll(percent, seconds):
        readings.append(percent)
        return service.poll_target(
            db_session, target, None, frame=frame, now=start + timedelta(seconds=seconds),
            titles=[],  # без цього прохід іде в мережу по заголовки вікон
        )

    yield poll, tmp_path
    service.reset_state_for_tests()


def test_single_frame_jump_to_100_is_not_shown(rig):
    poll, tmp_path = rig
    poll(50, 0)
    state = poll(100, 6)
    assert state.percent == 50
    card = service.MachineCard(target=state.target, state=state, now=state.frame_at)
    assert card.percent == 50  # саме це число бачить табло
    # Кадр, на якому детектор помилився, відкладено для розбору.
    assert (tmp_path / "percent_jump" / "10.0.0.81-8765.png").exists()
    state = poll(51, 12)
    assert state.percent == 51


def test_flicker_never_reaches_the_board(rig):
    poll, _ = rig
    shown = [poll(p, i * 6).percent for i, p in enumerate([50, 100, 51, 100, 51, 100])]
    assert shown == [50, 50, 51, 51, 51, 51]


def test_jump_confirmed_by_the_next_frame_is_accepted(rig):
    poll, _ = rig
    poll(20, 0)
    assert poll(80, 6).percent == 20
    assert poll(81, 12).percent == 81


def test_jump_after_a_long_gap_is_accepted_at_once(rig):
    """Верстат довго не відповідав — порівнювати нема з чим."""
    poll, _ = rig
    poll(20, 0)
    gap = service.PERCENT_JUMP_TRUST_GAP_SECONDS + 1
    assert poll(80, gap).percent == 80


def test_small_steps_and_drops_are_untouched(rig):
    poll, _ = rig
    assert [poll(p, i * 6).percent for i, p in enumerate([95, 99, 100, 0, 3])] == [95, 99, 100, 0, 3]


def test_reading_after_no_bar_is_taken_as_is(rig):
    """Нова програма після підсумку: попереднього числа немає."""
    poll, _ = rig
    poll(None, 0)
    assert poll(60, 6).percent == 60
