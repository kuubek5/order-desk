"""Історія показань верстатів: чи справді ми зберігаємо те, що вже читали.

Досі все, що агент читав з екрана, жило лише в памʼяті процесу і зникало на
рестарті. Через це на питання «скільки насправді фрезерувалась робота» в
системі не було відповіді: подій «у фрезеруванні» в базі нуль за всю історію,
а колонка «Відфрезерував» у таблиці містить ініціали людини, а не час.

Ці тести стережуть три речі, на яких така історія зазвичай і ламається:
подія мусить лягти ТИМ САМИМ кадром (інакше момент старту програми зʼїде),
потік не має писати рядок на кожен кадр, і відсутність сесії не має валити
опитування.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MachineReading
from app.services import machines as machines_service
from app.services.machines import (
    MACHINE_READING_HEARTBEAT_SECONDS,
    MachineState,
    MachineTarget,
    _should_store_machine,
    _store_machine_reading,
    prune_machine_readings,
)


@pytest.fixture(autouse=True)
def _clean_store_state():
    """Лічильник останнього запису живе в памʼяті процесу — між тестами його
    треба чистити, інакше вони бачать чужі кадри."""
    machines_service._stored.clear()
    yield
    machines_service._stored.clear()


def make_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def make_state(**overrides) -> MachineState:
    target = MachineTarget(
        name="350i", host="192.168.1.50", port=8765, agent_token="t", machine_id=1
    )
    state = MachineState(target=target)
    state.percent = overrides.pop("percent", 40)
    state.iso_name = overrides.pop("iso_name", "2026-09-08_18-13-22.iso")
    state.sum3d_id = overrides.pop("sum3d_id", "18-13-22")
    state.program_at = overrides.pop("program_at", datetime(2026, 9, 8, 18, 13, 22))
    state.layer = overrides.pop("layer", None)
    state.layers_total = overrides.pop("layers_total", None)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_first_reading_is_always_stored():
    state = make_state()
    assert _should_store_machine(state, datetime(2026, 9, 8, 18, 14)) is True


def test_a_new_program_is_stored_on_the_very_same_frame():
    """Головне правило: зміна програми — це межа роботи.

    Якби вона чекала наступного тіку, момент старту фрезерування в історії
    зʼїхав би на десяток секунд, і саме той час ми потім і рахуємо.
    """
    db = make_session()
    now = datetime(2026, 9, 8, 18, 14, 0)
    state = make_state()
    _store_machine_reading(db, state, now)

    # Той самий кадр через секунду — писати нема чого.
    assert _should_store_machine(state, now + timedelta(seconds=1)) is False

    # Нова програма — негайно, не чекаючи хвилини.
    state.iso_name = "2026-09-08_19-02-10.iso"
    state.sum3d_id = "19-02-10"
    assert _should_store_machine(state, now + timedelta(seconds=1)) is True


def test_percent_alone_does_not_write_a_row_every_frame():
    """Відсоток — потік, а не подія. Інакше кадр раз на кілька секунд дав би
    десятки тисяч рядків на добу заради числа, що повзе."""
    db = make_session()
    now = datetime(2026, 9, 8, 18, 14, 0)
    state = make_state(percent=40)
    _store_machine_reading(db, state, now)

    state.percent = 41
    assert _should_store_machine(state, now + timedelta(seconds=5)) is False
    state.percent = 77
    assert _should_store_machine(state, now + timedelta(seconds=30)) is False

    # Але «я живий» раз на хвилину лишається.
    later = now + timedelta(seconds=MACHINE_READING_HEARTBEAT_SECONDS + 1)
    assert _should_store_machine(state, later) is True


def test_slm_layer_change_counts_as_an_event():
    """У SISMA немає ні смуги, ні .iso — шар це єдиний його рух."""
    db = make_session()
    now = datetime(2026, 9, 8, 18, 14, 0)
    state = make_state(iso_name=None, sum3d_id=None, layer=250, layers_total=1049)
    _store_machine_reading(db, state, now)

    state.layer = 251
    assert _should_store_machine(state, now + timedelta(seconds=2)) is True


def test_stored_row_carries_the_key_to_the_queue_row():
    """Заради чого все: Sum3D ID і час програми мають лягти в базу, бо саме
    вони звʼязують екран верстата з рядком черги."""
    db = make_session()
    now = datetime(2026, 9, 8, 18, 14, 0)
    _store_machine_reading(db, make_state(), now)

    row = db.scalars(select(MachineReading)).one()
    assert row.host == "192.168.1.50-8765"
    assert row.sum3d_id == "18-13-22"
    assert row.iso_name == "2026-09-08_18-13-22.iso"
    assert row.program_at == datetime(2026, 9, 8, 18, 13, 22)
    assert row.percent == 40
    assert row.error is None


def test_error_row_keeps_the_trace_and_clears_the_numbers():
    """Мовчання не відрізнити від справності, тож слід невдачі теж пишемо —
    але БЕЗ чисел: хибне число тут гірше за жодне."""
    db = make_session()
    now = datetime(2026, 9, 8, 18, 14, 0)
    _store_machine_reading(db, make_state(), now, error="Верстат не відповів за 20 с")

    row = db.scalars(select(MachineReading)).one()
    assert row.error == "Верстат не відповів за 20 с"
    assert row.percent is None
    assert row.sum3d_id is None
    assert row.iso_name is None


def test_missing_session_never_breaks_the_poll():
    """`poll_target` законно кличуть і без БД (разовий знімок, тести).
    Кадр на екрані важливіший за рядок історії."""
    _store_machine_reading(None, make_state(), datetime(2026, 9, 8, 18, 14))


def test_prune_drops_only_rows_past_the_window():
    db = make_session()
    now = datetime(2026, 9, 8, 12, 0)
    fresh = now - timedelta(days=5)
    stale = now - timedelta(days=40)
    _store_machine_reading(db, make_state(), fresh)
    machines_service._stored.clear()
    _store_machine_reading(db, make_state(iso_name="old.iso"), stale)

    assert db.scalar(select(MachineReading).where(MachineReading.captured_at == stale)) is not None
    removed = prune_machine_readings(db, now=now)
    assert removed == 1
    left = db.scalars(select(MachineReading)).all()
    assert [row.captured_at for row in left] == [fresh]
