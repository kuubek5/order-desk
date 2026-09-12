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


NEWGEN_250I = Path(__file__).parent / "fixtures" / "newgen_progress_30.png"


def _rubbed_out(path: Path, box=(525, 305, 545, 330)) -> Image.Image:
    """Той самий кадр 250i, але одну цифру часу замальовано кольором тла.

    Той самий прийом, що в `test_machine_newgen_job._paint`: екран JOBS
    лишається собою (рядок ▶ на місці), а назва програми читатись перестає —
    рівно той випадок, заради якого гачок і існує.
    """
    from PIL import ImageDraw

    image = Image.open(path).convert("RGB")
    ImageDraw.Draw(image).rectangle(box, fill=(66, 66, 66))
    return image


def test_an_unread_newgen_screen_lands_in_the_inbox(db_session, tmp_path, monkeypatch):
    """Екран JOBS видно, а назву програми не прочитано — рядок у скриньці.

    Гачок доводиться ОКРЕМО від читача: `machine_newgen_job` перевірений сам по
    собі, але це не каже нічого про те, чи хтось його відмову доносить до
    людини. Аудит 12.09.26 показав, що виклик `screen_inbox.note(...,
    reason="newgen_unread")` у `_program_from_screen` можна видалити — і весь
    прогін лишиться зеленим. Тобто єдиний канал, яким кадр для донавчання
    шрифту доходить до скриньки, тримався ні на чому: лог глушиться до раза на
    годину, а кадр у `machine_frames/newgen_unread/` перезаписується наступним.

    Причина читача їде в `detail` дослівно (там номер символу, що не взявся) —
    інакше в скриньці лежав би кадр без жодної підказки, що саме донавчати.

    Сесія СПРАВЖНЯ навмисно, як і в сусідньому тесті верстата: з `db=None`
    захоплення тихо нічого не робить, і гачок був би невидимий.
    """
    from app.services import machines, screen_inbox

    monkeypatch.setattr(screen_inbox, "root", lambda: tmp_path / "puzzles")
    monkeypatch.setattr(machines, "save_frame", lambda *a, **k: None)
    # Кадр невпізнаного екрана `_report_unread_screen` кладе на диск — у tmp.
    monkeypatch.setattr(machines, "frames_root", lambda: tmp_path / "frames")
    screen_inbox.reset_state_for_tests()

    target = machines.MachineTarget(name="250i", host="10.0.0.81", port=5900)
    try:
        machines.poll_target(db_session, target, password=None, frame=_rubbed_out(NEWGEN_250I))
    finally:
        with machines._states_lock:
            machines._states.clear()

    rows = db_session.scalars(select(ScreenPuzzle)).all()
    unread = [r for r in rows if r.reason == "newgen_unread"]
    assert len(unread) == 1, [(r.reason, r.detail) for r in rows]
    assert unread[0].kind == screen_inbox.KIND_MACHINE
    assert unread[0].device_name == "250i"
    # Причина читача дослівно: «символ №…» — без неї донавчати нема за чим.
    assert "№" in (unread[0].detail or ""), unread[0].detail
    # І кадр збережено — саме з нього вчать еталон.
    assert (
        screen_inbox.folder(unread[0].kind, unread[0].device_key) / unread[0].frame_file
    ).exists()
    screen_inbox.reset_state_for_tests()


FURNACE_FRAMES = sorted((Path(__file__).resolve().parents[1] / "design" / "furnace-frames").glob("*.png"))
FURNACE_STATES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "furnace"


def test_a_furnace_does_not_collapse_into_a_single_puzzle(db_session, inbox, target):
    """Піч: РІЗНІ відмови — різні рядки, а не один на всю піч назавжди.

    Перша версія брала спільний поріг несхожості з верстатів (6.0). На екрані
    печі це вбивало фічу: панель завжди та сама, і ВСІ 14 цехових кадрів
    лежать у межах 3.54 один від одного — тобто будь-який наступний кадр
    «впізнавався» як уже відомий, скринька печі мала один рядок назавжди, а
    кнопка «це неважливо» на ньому глушила піч цілком.

    Тепер поріг свій на вид пристрою (піч 1.0), а порівняння йде лише в межах
    ОДНІЄЇ причини: інша відмова — інший рядок, навіть якщо картинка та сама.
    """
    from app import furnace_ocr

    with Image.open(REAL_FRAME) as opened:
        frame = opened.convert("RGB")
    real_read = furnace_ocr.read_panel

    def broken_temp(image):
        """Той самий кадр, але єдина відмова — невпізнана цифра температури.

        Обрізану «Команду» знімаємо навмисно: причина на кадр одна й береться
        за пріоритетом, а `zone_clipped` стоїть вище за `glyph_unknown`.
        """
        reading = real_read(image)
        for read in reading.fields.values():
            read.clipped = False
            read.text = read.raw or "0"
            read.unknown = 0
        reading.fields["temp"].text = None
        reading.fields["temp"].unknown = 1
        reading.fields["temp"].raw = "1?6"
        return reading

    # 1. Як є: обрізана зона «Команда».
    furnace.poll_target(db_session, target, password=None, frame=frame)
    # 2. Той самий кадр, але читач спіткнувся ІНАКШЕ — немає еталона цифри.
    screen_inbox.reset_state_for_tests()
    monkey = broken_temp
    furnace.read_panel, saved = monkey, furnace.read_panel
    try:
        furnace.poll_target(db_session, target, password=None, frame=frame)
    finally:
        furnace.read_panel = saved

    rows = db_session.scalars(select(ScreenPuzzle)).all()
    assert {r.reason for r in rows} == {"zone_clipped", "glyph_unknown"}, [
        (r.reason, r.detail) for r in rows
    ]


def test_two_furnace_states_are_two_screens(db_session, inbox, target):
    """RUN і WAIT — різні екрани печі, попри те що панель одна.

    Числа: та сама панель із іншими цифрами дає ≤0.77, RUN проти WAIT — 1.45.
    Поріг печі (1.0) стоїть саме між ними, і цей тест тримає його там.
    """
    run, wait = FURNACE_STATES / "run.png", FURNACE_STATES / "wait.png"
    with Image.open(run) as a, Image.open(wait) as b:
        gap = screen_inbox.distance(
            screen_inbox.signature(a.convert("RGB")), screen_inbox.signature(b.convert("RGB"))
        )
    limit = screen_inbox.NOVELTY_THRESHOLD[screen_inbox.KIND_FURNACE]
    assert gap > limit, f"RUN і WAIT злились ({gap:.2f}, поріг {limit})"

    # А та сама панель із іншими числами — один екран.
    with Image.open(FURNACE_FRAMES[0]) as a, Image.open(FURNACE_FRAMES[-1]) as b:
        same = screen_inbox.distance(
            screen_inbox.signature(a.convert("RGB")), screen_inbox.signature(b.convert("RGB"))
        )
    assert same <= limit, f"два кадри однієї панелі розійшлись на {same:.2f}"
