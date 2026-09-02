"""Читання відсотка зі смуги прогресу RemiCORE (Фаза 2 верстатів).

Принцип той самий, що на пічках: краще НІЧОГО, ніж хибне число. Тому тут
однаково важливі два боки — що правильна смуга читається точно, і що на
сміттєвому кадрі детектор мовчить.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from app.machine_ocr import find_progress_bar, read_progress_percent

BLUE = (0, 0, 128)      # виміряно на реальному кадрі 02.09.26
LIGHT = (255, 255, 255)  # порожня частина смуги — ЧИСТО біла
DARK = (40, 40, 40)
GREY_BG = (240, 240, 240)  # фон панелі RemiCORE (НЕ біла!)


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


def test_label_drawn_over_the_bar_does_not_break_reading():
    """ПРИЧИНА бойового промаху 02.09.26: RemiCORE малює підпис («9 %») ПОВЕРХ
    смуги, тож суцільного синього пробігу не існує — він розірваний літерами.
    Детектор мусить міряти РОЗМАХ синього, а не найдовший пробіг."""
    img = _screen()
    _draw_bar(img, box=(400, 780, 700, 800), percent=80)
    # «текст» — світлі прямокутники поверх заливки, як літери підпису
    d = ImageDraw.Draw(img)
    for x in (520, 536, 552):
        d.rectangle([x, 785, x + 7, 795], fill=(255, 255, 255))
    got = read_progress_percent(img)
    assert got is not None and abs(got - 80) <= 2, got


def test_label_over_the_EMPTY_part_does_not_shrink_the_bar():
    """Найпідступніший випадок (спіймано на реальному кадрі): коли заливка
    мала, підпис «9 %» стоїть на БІЛІЙ частині смуги. Сканування, що спиняється
    на першому не-білому пікселі, обривалось на літері — і 9% читались як 29%.
    Дірку завширшки з літеру треба перестрибувати."""
    for want in (9, 25, 50):
        img = _screen()
        _draw_bar(img, box=(458, 770, 586, 790), percent=want)
        d = ImageDraw.Draw(img)
        for x in range(512, 532, 3):  # «підпис» посеред смуги
            d.rectangle([x, 776, x + 1, 783], fill=(0, 0, 0))
        got = read_progress_percent(img)
        assert got is not None and abs(got - want) <= 2, (want, got)


def test_full_bar_on_white_panel_reads_100_not_85():
    """Бойовий випадок 03.09.26: смуга залита ПОВНІСТЮ, а панель RemiCORE
    навколо теж біла. Пропуск «дірок завширшки з літеру» (потрібний для
    підпису) перестрибував ще й РАМКУ смуги — і сканування їхало далі по білому
    тлу, роздуваючи контейнер: 100% читались як 85%.

    Рамка відрізняється від літери тим, що темна на ВСЮ висоту смуги."""
    for bg in ((255, 255, 255), (240, 240, 240)):
        for want in (9, 50, 85, 100):
            img = _screen(bg=bg)
            d = ImageDraw.Draw(img)
            left, top, right, bottom = 458, 770, 586, 790
            d.rectangle([left - 1, top - 1, right, bottom], outline=(64, 64, 64))
            d.rectangle([left, top, right - 1, bottom - 1], fill=LIGHT)
            fw = round((right - left) * want / 100)
            if fw:
                d.rectangle([left, top, left + fw - 1, bottom - 1], fill=BLUE)
            for x in range(left + 50, left + 72, 3):  # підпис поверх
                d.rectangle([x, 776, x + 1, 783], fill=(0, 0, 0))
            got = read_progress_percent(img)
            assert got is not None and abs(got - want) <= 2, (bg, want, got)


def test_edge_sliver_does_not_steal_the_last_percent():
    """Завершена програма показувала 99% замість 100%: між заливкою й рамкою
    лишався згладжений край в 1-2px. На смузі ~128px це <2%, тобто нижче
    роздільності самої смуги — рахуємо як повну. Але справжні неповні
    (85/90/95%) НЕ мають від цього стати сотнею."""
    def bar(percent, edge_px=0):
        img = _screen(bg=LIGHT)
        d = ImageDraw.Draw(img)
        left, top, right, bottom = 458, 770, 586, 790
        d.rectangle([left - 1, top - 1, right, bottom], outline=(64, 64, 64))
        d.rectangle([left, top, right - 1, bottom - 1], fill=LIGHT)
        fw = round((right - left) * percent / 100) - edge_px
        if fw > 0:
            d.rectangle([left, top, left + fw - 1, bottom - 1], fill=BLUE)
        return read_progress_percent(img)

    for edge in (0, 1, 2):
        assert bar(100, edge) == 100, edge
    for want in (85, 90, 95):
        got = bar(want)
        assert got is not None and abs(got - want) <= 2, (want, got)


def test_grey_panel_background_is_not_counted_as_empty_bar():
    """Друга причина: фон панелі сірий (240), а порожня частина смуги біла
    (255). З м'яким порогом «світлого» контейнер розповзався по всій панелі й
    відсоток виходив мізерним."""
    img = _screen()  # фон 240 на весь екран
    _draw_bar(img, box=(400, 780, 700, 800), percent=90)
    got = read_progress_percent(img)
    assert got is not None and abs(got - 90) <= 2, got


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


# ── Сторож на РЕАЛЬНОМУ кадрі верстата ────────────────────────────────────

def test_real_remicore_frame_reads_100_percent():
    """Кадр із бойового 350i (03.09.26), програма завершена — на екрані 100%.

    Цей файл ловить те, чого синтетика не ловила ЖОДНОГО разу:
    * найдовший синій пробіг у кадрі — це панель задач Windows (заввишки 2px),
      а не смуга: детектор мусить перебрати кандидатів, а не здатись на першому;
    * ліворуч від смуги стоїть окремий СИНІЙ напис «100%», тож міряти «розмах
      синього в рядку» не можна — лише суцільний пробіг;
    * у таблиці інструментів повно синіх цифр та іконок.

    Через ці три пастки детектор колись показував 741px «смугу» через пів
    екрана і видавав 85%/99% замість 100%.
    """
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "remicore_bar_100.png"
    bar = find_progress_bar(Image.open(path))
    assert bar is not None, "смугу на реальному кадрі не знайдено"
    assert bar.percent == 100, (bar.percent, bar)
    # Смуга, а не пів екрана: реальна ширина ~122px.
    assert 100 <= bar.container_width <= 200, bar
