"""Читач екрана SISMA — на БОЙОВИХ кадрах, не на намальованих.

Усі три фікстури зняті агентом із верстата 06.09.26 (`192.168.1.27`):
простій, друк (лазер пише шар) і пауза між шарами (машина розрівнює порошок,
лазер у цей момент `Enabled`). Саме третій випадок і змінив логіку: спершу
«Enabled» вважалось простоєм, і робота показувалась би як зупинена щоразу,
коли машина наносить порошок.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from app.machine_sisma import read_sisma, read_laser_word, screen_is_sisma

FIXTURES = Path(__file__).parent / "fixtures"


def _frame(name: str) -> Image.Image:
    return Image.open(FIXTURES / name)


def test_reads_layers_and_finish_time_while_printing():
    """Головне, заради чого все це: шар N з M і коли машина закінчить.

    Час завершення беремо з екрана, а не рахуємо з відсотка: машина знає
    свій темп, а наша оцінка «минуло/частка» брехала б на паузах."""
    reading = read_sisma(_frame("sisma_printing_250.png"))

    assert reading.printing is True
    assert reading.lasing is True
    assert (reading.layer, reading.layers_total) == (250, 1049)
    assert reading.percent == 24
    assert reading.started_at == datetime(2026, 9, 6, 15, 34)
    assert reading.ends_at == datetime(2026, 9, 6, 20, 58)


def test_recoating_between_layers_is_still_printing():
    """Бойовий кадр: лазер `Enabled`, але рядок шару живий — машина розрівнює
    порошок. Це РОБОТА. Три з 23 робочих кадрів саме такі, тож помилка тут
    показувала б «стоїть» кілька разів на годину."""
    reading = read_sisma(_frame("sisma_recoating_253.png"))

    assert reading.printing is True, "пауза між шарами — не простій"
    assert reading.lasing is False
    assert reading.laser_word == "Enabled"
    assert reading.layer == 253


def test_idle_screen_has_no_layers_and_no_times():
    reading = read_sisma(_frame("sisma_idle.png"))

    assert reading.printing is False
    assert reading.lasing is False
    assert reading.laser_word == "Enabled"
    assert reading.layer is None and reading.layers_total is None
    assert reading.ends_at is None
    assert reading.percent is None


def test_percent_comes_from_numbers_not_geometry():
    """Відсоток рахується з прочитаних чисел, тому точний і перевіряється
    арифметикою, а не «схоже на правду»."""
    reading = read_sisma(_frame("sisma_printing_250.png"))
    assert reading.percent == round(250 * 100 / 1049)


def test_laser_word_is_read_whole_and_only_when_exact():
    assert read_laser_word(_frame("sisma_printing_250.png")) == "Emitting"
    assert read_laser_word(_frame("sisma_idle.png")) == "Enabled"


def test_screen_is_sisma_recognises_both_states():
    assert screen_is_sisma(_frame("sisma_idle.png")) is True
    assert screen_is_sisma(_frame("sisma_printing_250.png")) is True


def test_foreign_screen_says_nothing():
    """RemiCORE — не SISMA. Читач мусить мовчати, а не вигадувати шари:
    два верстати різних поколінь стоять в одному цеху й опитуються однаково."""
    remicore = _frame("remicore_caption_72.png")

    assert screen_is_sisma(remicore) is False
    reading = read_sisma(remicore)
    assert reading.layer is None
    assert reading.ends_at is None
    assert reading.printing is None, "не наш екран — стан не називаємо"


def test_broken_frame_never_raises():
    """Опитування верстатів не має падати через кадр: збій читання = порожньо."""

    class Boom:
        size = (1280, 1024)

        def convert(self, *_a, **_k):
            raise OSError("кадр побився")

        def crop(self, *_a, **_k):
            return self

    reading = read_sisma(Boom())
    assert reading.printing is None and reading.layer is None


@pytest.mark.parametrize("name", ["sisma_printing_250.png", "sisma_recoating_253.png"])
def test_finish_time_is_the_same_on_both_working_frames(name):
    """Час завершення не має стрибати між фазами — інакше за ним не можна
    планувати зміну."""
    assert read_sisma(_frame(name)).ends_at == datetime(2026, 9, 6, 20, 58)


def test_card_shows_layers_and_finish_time_only_while_fresh():
    """Картка показує шари й час кінця лише зі СВІЖОГО кадру. Протухлий кадр
    мовчить: «закінчить о 20:58» з учорашнього друку — саме те хибне число,
    за яким планують зміну."""
    from datetime import timedelta
    from types import SimpleNamespace

    from app.services import machines as service

    now = datetime(2026, 9, 6, 17, 0)
    state = service.MachineState(
        target=SimpleNamespace(key="k", name="SISMA", host="h", port=8765),
        frame_at=now,
        percent=24,
        percent_at=now,
        is_sisma=True,
        layer=250,
        layers_total=1049,
        ends_at=datetime(2026, 9, 6, 20, 58),
        lasing=True,
    )
    card = service.MachineCard(target=state.target, state=state, now=now)
    assert card.layers == (250, 1049)
    assert card.ends_at == datetime(2026, 9, 6, 20, 58)
    assert card.phase_text == "пише шар"

    stale = service.MachineCard(
        target=state.target, state=state, now=now + timedelta(hours=3)
    )
    assert stale.layers is None
    assert stale.ends_at is None, "протухлий кадр не має обіцяти час завершення"


def test_card_calls_recoating_a_phase_not_a_stop():
    from types import SimpleNamespace

    from app.services import machines as service

    now = datetime(2026, 9, 6, 17, 0)
    state = service.MachineState(
        target=SimpleNamespace(key="k", name="SISMA", host="h", port=8765),
        frame_at=now, percent=24, percent_at=now, is_sisma=True,
        layer=253, layers_total=1049, lasing=False,
    )
    card = service.MachineCard(target=state.target, state=state, now=now)
    assert card.phase_text == "розрівнює порошок"
    assert card.is_running is True, "між шарами робота триває"


def test_card_template_renders_the_layer_line():
    """Роут-є-значить-готово тут не рахується: перевіряємо, що шаблон справді
    малює шари, час і фазу — а не що властивість повертає число."""
    from types import SimpleNamespace

    from app.routers.deps import templates
    from app.services import machines as service

    now = datetime(2026, 9, 6, 17, 0)
    state = service.MachineState(
        target=SimpleNamespace(key="k", name="SISMA", host="192.168.1.27", port=8765,
                               portrait_model="", machine_id=1),
        frame_at=now, percent=24, percent_at=now, is_sisma=True,
        layer=250, layers_total=1049, lasing=True,
        ends_at=datetime(2026, 9, 6, 20, 58),
    )
    card = service.MachineCard(target=state.target, state=state, now=now)
    html = templates.env.get_template("_machine_cards.html").render(
        request=None, cards=[card], user=SimpleNamespace(role="адмін"),
        calibration={"active": False},
    )

    assert "шар <b class=\"mono\">250</b>" in html
    assert "1049" in html
    assert "закінчить о <b class=\"mono\">20:58</b>" in html
    assert "пише шар" in html


def test_printer_leaves_the_milling_strip_and_gets_its_own_widget(monkeypatch):
    """SLM-принтер не стоїть в одному ряду з фрезерними: у них відсоток
    програми, у нього шари й час завершення (рішення власника 06.09.26)."""
    from types import SimpleNamespace

    from app.services import machines as service

    now = datetime(2026, 9, 6, 17, 0)

    def card(is_sisma):
        state = service.MachineState(
            target=SimpleNamespace(key="k", name="X", host="h", port=8765,
                                   portrait_model="", machine_id=1),
            frame_at=now, is_sisma=is_sisma,
        )
        return service.MachineCard(target=state.target, state=state, now=now)

    printer, mill = card(True), card(False)
    monkeypatch.setattr(service, "snapshot", lambda db: [printer, mill])
    monkeypatch.setattr(service, "strip_summary", lambda db: {})

    assert service.machine_side_context(None)["machine_cards"] == [mill]
    assert service.sisma_context(None)["sisma_cards"] == [printer]


def test_printer_stays_in_its_widget_even_without_a_link():
    """Обрив звʼязку не має ховати принтер: саме тоді він і потрібен."""
    from types import SimpleNamespace

    from app.services import machines as service

    now = datetime(2026, 9, 6, 17, 0)
    state = service.MachineState(
        target=SimpleNamespace(key="k", name="SISMA", host="h", port=8765,
                               portrait_model="", machine_id=1),
        frame_at=now - timedelta(hours=2), is_sisma=True, error="агент мовчить",
        error_at=now, fail_streak=5,
    )
    card = service.MachineCard(target=state.target, state=state, now=now)

    assert card.is_sisma_machine is True, "лишається у своєму віджеті"
    assert card.is_sisma is False, "але даних з протухлого кадру не показуємо"
    assert card.layers is None and card.ends_at is None
