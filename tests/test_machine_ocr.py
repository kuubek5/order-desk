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


def test_small_percent_is_read_not_dropped():
    """Бойовий випадок 03.09.26: щойно запущена програма показувала «не йде».
    Смуга ~122px, тобто 1% ≈ 1.2px — старий поріг ширини заливки (12px)
    відкидав УСЕ нижче 10%. Дрібні сині плями відсіює не поріг, а вимога
    впертись у рамку смуги."""
    for want in (3, 5, 9, 15):
        img = _screen()
        _draw_bar(img, box=(461, 776, 583, 795), percent=want)
        got = read_progress_percent(img)
        assert got is not None and abs(got - want) <= 2, (want, got)


def test_tall_label_inside_the_bar_is_not_mistaken_for_the_border():
    """Бойовий випадок 03.09.26: 23% читались як 59%. Підпис («23 %») на
    короткій смузі закривав більшість її висоти й проходив за рамку — скан
    спинявся на ньому, контейнер виходив коротким. Справжня рамка темна ще й
    ЗА межами заливки (це прямокутник), підпис — ні."""
    for want in (23, 40, 60):
        img = _screen()
        left, top, right, bottom = 461, 776, 583, 795
        _draw_bar(img, box=(left, top, right, bottom), percent=want)
        d = ImageDraw.Draw(img)
        cx = (left + right) // 2 - 12
        for x in range(cx, cx + 24, 3):       # високий підпис посеред смуги
            d.rectangle([x, top + 4, x + 1, bottom - 4], fill=(0, 0, 0))
        got = read_progress_percent(img)
        assert got is not None and abs(got - want) <= 3, (want, got)


def test_gcode_selection_block_never_wins_over_the_real_bar():
    """Бойові кадри 03.09.26: у RemiCORE поверх екрана відкрите вікно G-коду, і
    поточний блок рядків у ньому виділений СУЦІЛЬНИМ СИНІМ — прямокутник
    ~215px завширшки й сотні пікселів заввишки. За довжиною пробігу він
    перемагає справжню заливку (60-80px) із запасом, тож єдине, що його
    відсіює, — форма: смуга прогресу невисока (~23px).

    Раніше межа стояла на 60px, і виділення пролазило рівно тоді, коли вікно
    G-коду опускалось нижче. Найкращий кандидат на скаргу «пише 59%, хоча
    насправді 23%»."""
    img = _screen()
    d = ImageDraw.Draw(img)
    # Вікно G-коду з виділеним блоком — ліворуч, дотягується до низу кадру.
    d.rectangle([14, 300, 229, 850], fill=BLUE)
    # Справжня смуга — праворуч унизу, як на всіх бойових кадрах.
    _draw_bar(img, box=(458, 775, 586, 795), percent=23)

    got = read_progress_percent(img)
    assert got is not None and abs(got - 23) <= 2, (
        f"детектор прочитав {got} замість 23 — схоже, взяв виділення G-коду"
    )


def test_tall_solid_blue_alone_reads_nothing():
    """Той самий блок БЕЗ смуги поруч мусить дати порожньо, а не число:
    хибний відсоток гірший за жоден (те саме правило, що на пічках)."""
    img = _screen()
    ImageDraw.Draw(img).rectangle([14, 300, 229, 850], fill=BLUE)
    assert read_progress_percent(img) is None


# ── Підпис усередині смуги («43%») як головніший сигнал ─────────────────────


def _fixture_frame():
    from pathlib import Path

    return Image.open(Path(__file__).parent / "fixtures" / "remicore_bar_100.png").convert("RGB")


def test_caption_is_read_from_the_real_frame():
    """Реальний кадр верстата: підпис читається й збігається з геометрією.

    Це і є сенс другого сигналу — незалежна перевірка того самого числа.
    Еталони цифр «1», «0» і знака «%» зняті саме з цього кадру.
    """
    from app.machine_ocr import find_progress_bar, read_caption_percent

    image = _fixture_frame()
    bar = find_progress_bar(image)
    assert bar is not None
    assert read_caption_percent(image, bar) == 100
    assert read_progress_percent(image) == 100


def test_caption_stays_silent_without_templates(monkeypatch):
    """Без еталонів підпис мовчить, а відсоток лишається геометричним.

    Це і є обіцянка сумісності: новий сигнал може лише виправити число, але
    ніколи не зробити гірше, ніж було до навчання.
    """
    from app import machine_ocr

    monkeypatch.setattr(machine_ocr, "load_machine_glyphs", lambda: {})
    image = _fixture_frame()
    bar = machine_ocr.find_progress_bar(image)
    assert machine_ocr.read_caption_percent(image, bar) is None
    assert machine_ocr.read_progress_percent(image) == bar.percent


def test_caption_refuses_unknown_glyph(monkeypatch):
    """Незнайоме накреслення — мовчання, а не здогадка.

    Правило модуля: хибне число гірше за жодне. Якщо хоч один символ підпису
    не збігся піксель-у-піксель, число з підпису не береться взагалі — а не
    складається з тих цифр, які впізнались (так «43» перетворилось би на «4»).
    """
    from app import machine_ocr

    # Еталони є, але від ІНШОГО шрифту: жоден символ не збіжиться.
    monkeypatch.setattr(
        machine_ocr, "load_machine_glyphs", lambda: {10: {"7": [["1" * 6] * 10]}}
    )
    image = _fixture_frame()
    bar = machine_ocr.find_progress_bar(image)
    assert machine_ocr.read_caption_percent(image, bar) is None


def test_caption_wins_over_geometry(monkeypatch):
    """Коли сигнали розходяться, береться підпис — верстат про себе знає краще."""
    from app import machine_ocr

    image = _fixture_frame()
    bar = machine_ocr.find_progress_bar(image)
    assert bar.percent == 100
    monkeypatch.setattr(machine_ocr, "read_caption_percent", lambda *_: 43)
    assert machine_ocr.read_progress_percent(image) == 43


def test_caption_mask_normalises_both_halves():
    """Маска зводить двоколірний підпис до одного вигляду.

    Підпис стоїть по центру КОНТЕЙНЕРА, тож на частковому прогресі він
    розрізаний межею заливки: ліворуч білий на синьому, праворуч темний на
    світлому. Без нормалізації одна з половин просто зникла б із маски.
    """
    from app.machine_ocr import caption_mask, find_progress_bar

    screen = _screen()
    box = (400, 700, 700, 723)
    _draw_bar(screen, box=box, percent=50)
    # Підпис поверх межі: ліва половина на заливці, права — поза нею.
    draw = ImageDraw.Draw(screen)
    draw.rectangle((535, 706, 545, 716), fill=LIGHT)   # білий шматок на синьому
    draw.rectangle((556, 706, 566, 716), fill=DARK)    # темний шматок на світлому

    bar = find_progress_bar(screen)
    assert bar is not None
    mask = caption_mask(screen, bar)
    assert mask is not None
    # Обидва шматки видно як чорне: ширина чорного більша за один із них.
    black_columns = {
        x for x in range(mask.width) for y in range(mask.height)
        if mask.getpixel((x, y)) == (0, 0, 0)
    }
    assert black_columns, "жоден шматок підпису не потрапив у маску"
    assert len(black_columns) >= 20, "у маску потрапила лише одна половина підпису"


def test_caption_reads_partial_fill_correctly():
    """Реальний кадр із ЧАСТКОВОЮ заливкою: підпис «72%» читається точно.

    Тут ловився баг caption_mask (04.09.26): маска ділила світле/темне по краю
    КОНТЕЙНЕРА замість краю ЗАЛИВКИ, тож порожня біла частина смуги йшла в маску
    як текст. На 100% (fill==container) це збігалось, тому баг не було видно —
    аж до калібрування реальними частковими кадрами. Геометрія тут дає 70,
    підпис — правдиве 72; саме заради цієї різниці підпис і головніший.
    """
    from pathlib import Path

    from app.machine_ocr import (
        find_progress_bar,
        read_caption_percent,
        read_progress_percent,
    )

    path = Path(__file__).parent / "fixtures" / "remicore_caption_72.png"
    image = Image.open(path).convert("RGB")
    bar = find_progress_bar(image)
    assert bar is not None
    assert bar.percent == 70, "геометрія на цьому кадрі занижує (для того й підпис)"
    assert read_caption_percent(image, bar) == 72
    assert read_progress_percent(image) == 72, "віддаємо ПІДПИС, не геометрію"


def test_caption_tolerates_percent_glyph_height_variant():
    """Підпис «57%», де знак «%» ВИЩИЙ за цифри (висота 13 проти 12).

    Реальний кадр .76 (04.09.26): «%» рендериться варіативно, і вимога точного
    збігу «%» відкидала цілий вірний підпис — читання давало None, а віджет
    падав на геометрію. Тепер цифри читаються зліва, а «%» (будь-якої висоти,
    у скількох завгодно сегментах) ігнорується. Тут геометрія й підпис збіглись
    на 57 — саме те, що показує сам верстат."""
    from pathlib import Path

    from app.machine_ocr import find_progress_bar, read_caption_percent

    path = Path(__file__).parent / "fixtures" / "remicore_caption_57.png"
    image = Image.open(path).convert("RGB")
    bar = find_progress_bar(image)
    assert bar is not None
    assert read_caption_percent(image, bar) == 57
