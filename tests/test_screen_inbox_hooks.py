"""Чи справді опитування кладе незрозумілий кадр у скриньку.

Сервіс `screen_inbox` перевіряється окремо (`test_screen_inbox.py`) — тут
питання інше й важливіше: чи ВИКЛИКАЄТЬСЯ він із живого шляху опитування. Саме
цього бракувало не раз: функція правильна, тести зелені, а в цеху вона не
спрацьовує жодного разу, бо умова виклику не та (журнал обривів, 09.09.26).

Тому вхід тут — справжній кадр печі з `design/furnace-frames/`, а не
намальований: на ньому зона «Команда» обрізана рамкою кропа, і саме це має
доїхати до скриньки разом із вирізом зони.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.models import ScreenPuzzle
from app.services import furnace, screen_inbox

REAL_FRAME = Path(__file__).resolve().parents[1] / "design" / "furnace-frames" / "frame (1).png"


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    """Скринька й кадри — у тимчасовій теці, стан процесу чистий."""
    monkeypatch.setattr(screen_inbox, "root", lambda: tmp_path / "puzzles")
    monkeypatch.setattr(furnace, "frames_root", lambda: tmp_path / "frames")
    screen_inbox.reset_state_for_tests()
    yield
    screen_inbox.reset_state_for_tests()


@pytest.fixture
def target():
    return furnace.FurnaceTarget(name="Піч 1", host="10.0.0.1", port=5900)


def test_unexpected_screen_size_lands_in_the_inbox(db_session, inbox, target):
    """Кадр не того розміру — «незнайомий розклад екрана», один рядок."""
    furnace.poll_target(
        db_session, target, password=None, frame=Image.new("RGB", (640, 480), (20, 20, 20))
    )

    rows = db_session.scalars(select(ScreenPuzzle)).all()
    assert len(rows) == 1
    assert rows[0].reason == "layout_unknown"
    assert "640×480" in rows[0].detail
    assert rows[0].kind == screen_inbox.KIND_FURNACE
    assert rows[0].device_name == "Піч 1"
    # Кадр збережено — інакше розбирати нема чого.
    assert (screen_inbox.folder(rows[0].kind, rows[0].device_key) / rows[0].frame_file).exists()


def test_the_same_screen_twice_does_not_grow_the_inbox(db_session, inbox, target):
    """Той самий екран удруге — той самий рядок.

    Кадр знімається раз на 6 с; без цього скринька за годину мала б 600 рядків
    на пристрій, і фіча загинула б у перший же день.
    """
    frame = Image.new("RGB", (640, 480), (20, 20, 20))
    furnace.poll_target(db_session, target, password=None, frame=frame)
    furnace.poll_target(db_session, target, password=None, frame=frame)

    assert db_session.scalar(select(ScreenPuzzle.id).order_by(ScreenPuzzle.id)) is not None
    assert len(db_session.scalars(select(ScreenPuzzle)).all()) == 1


def test_a_clipped_zone_on_a_real_frame_reaches_the_inbox_with_its_crop(
    db_session, inbox, target
):
    """Справжній кадр печі: зона «Команда» обрізана — і це видно у скриньці.

    Разом із кадром лягає виріз зони в РІДНОМУ масштабі: саме з нього вчать
    еталон, а зменшена копія растрового шрифту зробила б навчання вгадуванням.
    """
    with Image.open(REAL_FRAME) as opened:
        frame = opened.convert("RGB")
    furnace.poll_target(db_session, target, password=None, frame=frame)

    rows = db_session.scalars(select(ScreenPuzzle)).all()
    assert len(rows) == 1
    puzzle = rows[0]
    assert puzzle.reason == "zone_clipped"
    assert "Команда" in puzzle.detail
    zone_file = screen_inbox.image_path(puzzle, "zone")
    assert zone_file is not None
    with Image.open(zone_file) as crop:
        # Рівно прямокутник зони з `furnace_ocr.ZONES["command"]`.
        assert crop.size == (121, 20)


def test_a_readable_frame_leaves_the_inbox_empty(db_session, inbox, target, monkeypatch):
    """Кадр, прочитаний повністю, у скриньку не потрапляє.

    Інакше скринька перетворилась би на другий лог: вона має тримати рівно те,
    з чим до людини є питання.
    """
    from app import furnace_ocr

    with Image.open(REAL_FRAME) as opened:
        frame = opened.convert("RGB")

    real_read = furnace_ocr.read_panel

    def all_clear(image):
        reading = real_read(image)
        reading.warnings.clear()
        for read in reading.fields.values():
            read.text = read.raw or "0"
            read.clipped = False
            read.unknown = 0
        reading.status = furnace_ocr.STATUS_RUN
        return reading

    monkeypatch.setattr(furnace, "read_panel", all_clear)
    furnace.poll_target(db_session, target, password=None, frame=frame)

    assert db_session.scalars(select(ScreenPuzzle)).all() == []


def test_a_machine_frame_with_nothing_readable_lands_in_the_inbox(
    db_session, tmp_path, monkeypatch
):
    """Верстат: кадр є, а знялось із нього нічого — теж питання до людини.

    Так виглядає і робочий стіл, і невідомий діалог; розрізнити їх може лише
    людина, і саме для цього рядок із лічильником і кнопкою «неважливо».

    Сесія тут СПРАВЖНЯ навмисно: сусідні тести опитування кличуть
    `poll_target(None, …)`, і з `db=None` захоплення тихо нічого не робить
    (`note` ковтає будь-який виняток) — тобто на них гачок був би невидимий.
    """
    from app.services import machines, screen_inbox

    monkeypatch.setattr(screen_inbox, "root", lambda: tmp_path / "puzzles")
    monkeypatch.setattr(machines, "save_frame", lambda *a, **k: None)
    screen_inbox.reset_state_for_tests()

    target = machines.MachineTarget(name="Верстат 3", host="10.0.0.9", port=5900)
    machines.poll_target(
        db_session, target, password=None, frame=Image.new("RGB", (300, 200), (240, 240, 240))
    )
    with machines._states_lock:
        machines._states.clear()

    rows = db_session.scalars(select(ScreenPuzzle)).all()
    assert [r.reason for r in rows] == ["layout_unknown"]
    assert rows[0].kind == screen_inbox.KIND_MACHINE
    assert rows[0].device_name == "Верстат 3"
    screen_inbox.reset_state_for_tests()
