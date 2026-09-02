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


# ── Ім'я .iso із заголовка вікна: зв'язка верстат ↔ наряд ──────────────────

from app.machine_ocr import parse_iso_title, pick_milling_program  # noqa: E402

# Точний заголовок з кадру 02.09.26 — сторож проти регресій.
REAL_TITLE = "Remote - zr18_18-Monolith-A3-x62_2026-09-02_23-04-33.iso"


def test_parses_real_remicore_title():
    prog = parse_iso_title(REAL_TITLE)
    assert prog is not None
    assert prog.sum3d_id == "23-04-33"       # ключ до рядка черги
    assert prog.date == "2026-09-02"
    assert prog.iso_name == "zr18_18-Monolith-A3-x62_2026-09-02_23-04-33.iso"


def test_parse_is_case_insensitive_about_extension():
    assert parse_iso_title("x_2026-01-02_03-04-05.ISO").sum3d_id == "03-04-05"


def test_parse_rejects_unrelated_titles():
    for t in ["Проводник", "", "Remote - untitled.iso", "Notepad — 23-04-33"]:
        assert parse_iso_title(t) is None


def test_pick_ignores_other_windows():
    titles = ["Проводник", "Telegram", REAL_TITLE, "crm-setup.txt — Блокнот"]
    assert pick_milling_program(titles).sum3d_id == "23-04-33"


def test_pick_refuses_when_two_different_programs_open():
    # Два різні кандидати = не знаємо, який фрезерується → краще нічого.
    titles = [REAL_TITLE, "Remote - other_2026-09-02_11-11-11.iso"]
    assert pick_milling_program(titles) is None


def test_pick_allows_duplicate_windows_of_same_program():
    assert pick_milling_program([REAL_TITLE, REAL_TITLE]).sum3d_id == "23-04-33"


def test_pick_on_empty_input():
    assert pick_milling_program([]) is None
    assert pick_milling_program(None) is None
