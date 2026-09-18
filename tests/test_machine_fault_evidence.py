"""Помилка на екрані верстата: чи її видно, з якого часу й звідки.

Бойовий випадок 18.09.26 (350i Loader): уночі на екрані висів діалог
RemiCORE «Error … Reference run required!», власник підписав його у скриньці
невідомих «помилка, потрібно підійти». Канал заголовків вікон, який мав цей
діалог ловити, з 13.09 лишався неперевіреним наживо — і перевірити заднім
числом не було чим: ні стан «помилка», ні заголовки не видно ні в лозі, ні в
MCP. Тут стережемо, щоб наступний такий випадок лишив докази.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.models import ScreenPuzzle
from app.services import machines as ms
from app.services import screen_inbox

REMICORE_ERROR = Path(__file__).parent / "fixtures" / "remicore_error_dialog.png"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "save_frame", lambda *a, **k: None)
    monkeypatch.setattr(screen_inbox, "root", lambda: tmp_path / "puzzles")
    screen_inbox.reset_state_for_tests()
    target = ms.MachineTarget(name="350i Loader", host="10.0.0.85", port=8765, agent_token="t")
    yield target
    with ms._states_lock:
        ms._states.pop(target.key, None)
    screen_inbox.reset_state_for_tests()


def _frame():
    with Image.open(REMICORE_ERROR) as opened:
        return opened.convert("RGB")


def test_error_window_title_marks_the_machine_as_fault(agent):
    """Поточна поведінка (13.09.26): вікно «Error» серед заголовків = помилка."""
    state = ms.poll_target(None, agent, password=None, frame=_frame(), titles=["Remote", "Error"])
    assert state.fault is True


def test_fault_recognised_by_title_does_not_go_to_the_unknown_screens(db_session, agent):
    """Помилку вже впізнано — питати людину «що це за екран» нема чого.

    Доти умова скриньки дивилась лише на банер із кадру й не бачила заголовка,
    тож діалог RemiCORE падав у «невідомі» навіть коли стан був «помилка»."""
    ms.poll_target(db_session, agent, password=None, frame=_frame(), titles=["Remote", "Error"])
    assert db_session.scalars(select(ScreenPuzzle)).all() == []


def test_without_the_error_title_the_frame_still_asks(db_session, agent):
    """Заголовка «Error» нема — кадр і далі йде в скриньку, як було."""
    ms.poll_target(db_session, agent, password=None, frame=_frame(), titles=["Remote"])
    assert [p.reason for p in db_session.scalars(select(ScreenPuzzle))] == ["layout_unknown"]


def test_fault_start_and_end_are_remembered_and_logged(agent, caplog):
    start = datetime(2026, 9, 18, 1, 40)
    with caplog.at_level(logging.INFO, logger=ms.logger.name):
        ms.poll_target(None, agent, password=None, frame=_frame(), now=start,
                       titles=["Remote", "Error"])
        state = ms.poll_target(None, agent, password=None, frame=_frame(),
                               now=start + timedelta(seconds=6), titles=["Remote", "Error"])
        assert state.fault_since == start
        state = ms.poll_target(None, agent, password=None, frame=_frame(),
                               now=start + timedelta(minutes=5), titles=["Remote"])
    assert state.fault_since is None
    text = caplog.text
    assert text.count("помилка на екрані") == 1          # поява — рівно раз
    assert "заголовок вікна" in text                      # і звідки взято
    assert "помилку прибрано" in text


def test_devices_report_shows_fault_and_window_titles(agent, monkeypatch):
    from app.services import device_diag

    ms.poll_target(None, agent, password=None, frame=_frame(), titles=["Remote", "Error"])
    monkeypatch.setattr(ms, "configured_targets", lambda db: [agent])
    monkeypatch.setattr(device_diag.furnace, "snapshot", lambda db: [])
    monkeypatch.setattr(device_diag, "_unread_screens", lambda: [])
    report = device_diag.devices(None)
    mill = next(m for m in report["верстати"] if m["ключ"] == agent.key)
    assert mill["помилка_на_екрані"] is True
    assert mill["помилка_з"] is not None
    assert mill["заголовки_вікон"] == ["Remote", "Error"]
