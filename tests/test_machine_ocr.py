"""Читання відсотка зі смуги прогресу RemiCORE (Фаза 2 верстатів).

Принцип той самий, що на пічках: краще НІЧОГО, ніж хибне число. Тому тут
однаково важливі два боки — що правильна смуга читається точно, і що на
сміттєвому кадрі детектор мовчить.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from app.machine_ocr import find_progress_bar, read_progress_percent

BLUE = (59, 111, 212)
LIGHT = (245, 245, 245)
DARK = (40, 40, 40)
GREY_BG = (200, 200, 200)


def _screen(w=1152, h=864, bg=GREY_BG) -> Image.Image:
    return Image.new("RGB", (w, h), bg)


def _draw_bar(img: Image.Image, *, box, percent: int):
    """Смуга: рамка + світлий фон + синя заливка на `percent` ширини."""
    left, top, right, bottom = box
    d = ImageDraw.Draw(img)
    d.rectangle([left - 1, top - 1, right, bottom], outline=DARK)
    d.rectangle([left, top, right - 1, bottom - 1], fill=LIGHT)
    fill_w = round((right - left) * percent / 100)
    if fill_w > 0:
        d.rectangle([left, top, left + fill_w - 1, bottom - 1], fill=BLUE)


def test_reads_nine_percent_like_the_real_screen():
    # Реальний кадр 02.09.26: смуга внизу, заливка 9%.
    img = _screen()
    _draw_bar(img, box=(458, 775, 586, 795), percent=9)
    assert read_progress_percent(img) == 9


def test_reads_half_and_full():
    for want in (25, 50, 75, 100):
        img = _screen()
        _draw_bar(img, box=(400, 780, 700, 800), percent=want)
        got = read_progress_percent(img)
        assert got is not None and abs(got - want) <= 1, (want, got)


def test_bar_geometry_is_reported_for_proof():
    img = _screen()
    _draw_bar(img, box=(400, 780, 700, 800), percent=50)
    bar = find_progress_bar(img)
    assert bar is not None
    assert bar.container_width >= 290  # ~300 з рамкою
    assert 0 < bar.fill_width < bar.container_width
    left, top, right, bottom = bar.box
    assert right > left and bottom > top


def test_empty_bar_gives_zero_or_none_never_garbage():
    img = _screen()
    _draw_bar(img, box=(400, 780, 700, 800), percent=0)
    got = read_progress_percent(img)
    assert got is None or got == 0


def test_no_bar_returns_none():
    assert read_progress_percent(_screen()) is None


def test_ignores_blue_in_upper_half():
    # Синя підсвітка рядка інструмента вгорі екрана — НЕ смуга прогресу.
    img = _screen()
    ImageDraw.Draw(img).rectangle([300, 200, 900, 240], fill=BLUE)
    assert read_progress_percent(img) is None


def test_ignores_thin_blue_line():
    img = _screen()
    ImageDraw.Draw(img).rectangle([400, 780, 700, 781], fill=BLUE)  # 2px заввишки
    assert read_progress_percent(img) is None


def test_ignores_tiny_blue_icon():
    img = _screen()
    ImageDraw.Draw(img).rectangle([500, 770, 508, 790], fill=BLUE)  # вузька іконка
    assert read_progress_percent(img) is None


def test_works_on_scaled_frame():
    # Кадр міг бути масштабований — детектор не має залежати від роздільності.
    img = _screen(1920, 1080)
    _draw_bar(img, box=(760, 980, 1160, 1004), percent=60)
    got = read_progress_percent(img)
    assert got is not None and abs(got - 60) <= 2


def test_tiny_image_is_refused():
    assert read_progress_percent(Image.new("RGB", (40, 20), GREY_BG)) is None
