"""Знімок печі по справжньому RFB — від рукостискання до розпізнаних чисел.

Решта тестів печей підміняє `capture()` і перевіряє нашу логіку. Тут навпаки:
логіка мінімальна, а перевіряється весь шматок, який ми НЕ писали, — asyncvnc,
рукостискання, DES-автентифікація, розбір сирого фреймбуфера. Саме він
ламається від оновлення залежностей, і саме його не видно в моках.

Стенд — tests/fake_vnc_server.py, справжній сокет на 127.0.0.1.
"""

from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.furnace_ocr import STATUS_RUN, STATUS_WAIT
from app.furnace_vnc import FurnaceVncError, capture
from app.services import furnace as service
from tests.fake_vnc_server import FakeFurnace, frame_bytes

FIXTURES = Path(__file__).parent / "fixtures" / "furnace"


def _image(name: str) -> Image.Image:
    return Image.open(FIXTURES / f"{name}.png").convert("RGB")


def _furnace(name: str = "run", **kwargs) -> FakeFurnace:
    image = _image(name)
    return FakeFurnace(
        width=image.width, height=image.height, frame_rgba=frame_bytes(image), **kwargs
    )


@pytest.fixture(autouse=True)
def _clean_state():
    service.reset_state_for_tests()
    yield
    service.reset_state_for_tests()


def test_capture_returns_the_exact_frame_the_furnace_showed():
    """Кадр мусить доїхати байт у байт: одна переплутана пара каналів — і
    червона температура стала б синьою, а розпізнавання «осліпло» б."""
    with _furnace("run") as furnace:
        frame = capture("127.0.0.1", furnace.port, "DEKEMA")
    assert frame.size == (800, 600)
    assert frame.tobytes() == _image("run").tobytes()


def test_we_never_send_the_furnace_a_single_input_event():
    """Обіцянка «тільки перегляд» — тут вона стає перевіркою, а не коментарем.

    Заодно ловиться shared-прапорець: без нього наша сесія вибивала б
    оператора, який стоїть коло печі з RealVNC.
    """
    with _furnace("run") as furnace:
        capture("127.0.0.1", furnace.port, "DEKEMA")
        capture("127.0.0.1", furnace.port, "DEKEMA")
    assert furnace.input_events == []
    assert furnace.shared_flags == [1, 1]


def test_wrong_password_is_a_readable_message_not_a_traceback():
    # Пароль ASCII навмисно: VNC-автентифікація працює з байтами ASCII, і
    # кирилиця тут упала б у стенді, а не в тому, що ми перевіряємо.
    with _furnace("run", password="OTHERPWD") as furnace:
        with pytest.raises(FurnaceVncError) as exc:
            capture("127.0.0.1", furnace.port, "DEKEMA")
    assert "пароль" in str(exc.value)
    assert furnace.bad_passwords == 1


def test_a_silent_furnace_hits_the_deadline_instead_of_hanging_forever():
    """Напівживий сокет — причина, через яку в знімку взагалі є дедлайн:
    без нього фоновий потік завис би назавжди, як колись поштовий синк."""
    with _furnace("run", hang=True) as furnace:
        with pytest.raises(FurnaceVncError) as exc:
            capture("127.0.0.1", furnace.port, "DEKEMA", timeout=1.0)
    assert "не відповіла" in str(exc.value)


def test_closed_port_is_reported_as_unreachable():
    furnace = _furnace("run").start()
    port = furnace.port
    furnace.stop()
    with pytest.raises(FurnaceVncError) as exc:
        capture("127.0.0.1", port, "DEKEMA", timeout=3.0)
    assert "недоступна" in str(exc.value) or "не відповіла" in str(exc.value)


def test_end_to_end_a_real_socket_becomes_a_row_in_the_database(tmp_path, monkeypatch):
    """Увесь конвеєр без жодного мока: сокет → RFB → кадр → OCR → база."""
    monkeypatch.setattr(service, "frames_root", lambda: tmp_path)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)

    with _furnace("run") as furnace:
        target = service.FurnaceTarget(name="Стенд", host="127.0.0.1", port=furnace.port)
        with Session(engine) as db:
            state = service.poll_target(db, target, password="DEKEMA")
            assert state.status == STATUS_RUN
            assert state.temp_c == 759
            # 26:59 — «срок», залишок поточної команди; час відкриття рахується
            # із залишку УСІЄЇ програми (правий лічильник табло).
            assert state.reading.step_seconds == 26 * 60 + 59
            assert state.remaining_seconds == 28207

            # Піч перемкнулась у спокій — те саме підключення, інший кадр.
            furnace.frame_rgba = frame_bytes(_image("wait"))
            state = service.poll_target(db, target, password="DEKEMA")
            assert state.status == STATUS_WAIT
            assert state.temp_c == 40

    assert (tmp_path / f"127.0.0.1-{target.port}.png").exists()


def test_every_connection_is_closed_after_the_frame():
    """Піч тримає обмежене число сесій. Якщо ми лишатимемо їх відкритими,
    через добу опитування вона перестане пускати навіть оператора."""
    with _furnace("run") as furnace:
        for _ in range(5):
            capture("127.0.0.1", furnace.port, "DEKEMA")
        assert furnace.connections == 0
