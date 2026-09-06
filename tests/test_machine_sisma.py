"""Читач екрана SISMA — на БОЙОВИХ кадрах, не на намальованих.

Усі три фікстури зняті агентом із верстата 06.09.26 (`192.168.1.27`):
простій, друк (лазер пише шар) і пауза між шарами (машина розрівнює порошок,
лазер у цей момент `Enabled`). Саме третій випадок і змінив логіку: спершу
«Enabled» вважалось простоєм, і робота показувалась би як зупинена щоразу,
коли машина наносить порошок.
"""

from datetime import datetime
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
