"""Читання відсотка виконання зі знімка екрана RemiCORE (верстат, Фаза 2).

RemiCORE малює внизу горизонтальну СМУГУ прогресу: залита частина — синя,
порожня — світла, все в тонкій рамці. Відсоток читається з **геометрії смуги**
(частка заливки), а не OCR цифр: смуга — суцільна кольорова область на сотні
пікселів, тож вона незрівнянно надійніша за дрібний шрифт. Це та сама вимога,
що на пічках: **хибне число гірше за жодне** (app/furnace_ocr.py).

Детектор САМОКАЛІБРУВАЛЬНИЙ — шукає смугу за кольором і формою, а не за
фіксованими координатами. Причина практична: роздільність екранів верстатів
різна, кадр може бути масштабований, а зашиті координати тихо ламаються при
першій же зміні (урок 02.09.26 — див. project_machine_agent). Тому:

* синім вважається піксель, де синій канал різко переважає (виміряно на кадрі:
  чиста наві `(0, 0, 128)`);
* заливка — найдовший СУЦІЛЬНИЙ пробіг сині; саме пробіг, а не «розмах синього
  в рядку»: ліворуч від смуги стоїть окремий синій напис «100%», і розмах
  захоплював його разом із половиною екрана;
* кандидати перебираються за довжиною, доки не знайдеться той, що має ФОРМУ
  смуги (потрібну висоту): найдовший пробіг у кадрі — панель задач Windows
  заввишки 2px, тож здаватись на першому кандидаті не можна;
* далі один прохід управо: літери підпису (він буває і на заливці, і на
  порожній частині) пропускаються, а зупинка — на РАМЦІ смуги, яку від літери
  відрізняє те, що вона темна на всю висоту смуги;
* відсоток = ширина заливки / ширина контейнера.

Кожен із цих пунктів — слід реального промаху (85%, 99%, «смуга» на 741px);
`tests/fixtures/remicore_bar_100.png` — справжній кадр верстата як сторож.

Якщо смугу не знайдено або пропорції неправдоподібні — повертаємо None, і UI
просто не показує число.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from PIL import Image

# Смуга живе в нижній частині екрана RemiCORE (панель керування). Верхні 60%
# кадру не скануємо взагалі: там сині елементи інтерфейсу (кнопки, підсвітка
# рядка інструмента), і вони давали б хибні збіги.
BOTTOM_BAND = 0.60

# Мінімальна ширина заливки в пікселях: коротші сині плями — це іконки й текст.
MIN_FILL_WIDTH = 12
# Смуга мусить мати висоту (кілька однакових рядків поспіль), інакше це лінія.
MIN_BAR_HEIGHT = 4
# Правдоподібна геометрія контейнера: смуга прогресу широка й невисока.
MIN_CONTAINER_WIDTH = 40
MAX_BAR_HEIGHT = 60
# Найширша «дірка» в порожній частині, яку вважаємо підписом поверх смуги, а не
# її кінцем. Літера підпису — кілька пікселів; фон панелі тягнеться сотнями.
MAX_LABEL_GAP = 14
# Яка частка рядків смуги має бути темною, щоб вважати колонку РАМКОЮ, а не
# літерою підпису. Рамка йде на всю висоту, літера — на частину.
BORDER_DARK_SHARE = 0.7
# Скільки пікселів між заливкою й рамкою вважаємо згладженим краєм, а не
# залишком. На смузі ~128px це < 2%, тобто нижче її власної роздільності.
EDGE_SLACK = 2
# Скільки найдовших пробігів перевіряти на «форму смуги». Найдовший у кадрі —
# панель задач Windows (заввишки 2px), тож одним кандидатом не обійтись.
MAX_BAR_CANDIDATES = 40


def _is_blue(px: tuple[int, int, int]) -> bool:
    """Заливка смуги. Виміряно на реальному кадрі 02.09.26: чиста темна синь
    `(0, 0, 128)`, тобто r == g і синій різко переважає. Поріг узятий із запасом
    на згладжування країв і на світліші пікселі тексту поверх смуги."""
    r, g, b = px[0], px[1], px[2]
    return b >= 90 and b - r >= 40 and b - g >= 40


def _is_unfilled(px: tuple[int, int, int]) -> bool:
    """Порожня частина смуги — ЧИСТО БІЛА (255), а фон панелі RemiCORE сірий
    (240). Тому поріг саме 250, а не «світле взагалі»: з м'яким порогом
    контейнер розповзався по всій сірій панелі й відсоток виходив мізерним."""
    return px[0] >= 250 and px[1] >= 250 and px[2] >= 250


@dataclass(frozen=True)
class ProgressBar:
    percent: int
    fill_width: int
    container_width: int
    box: tuple[int, int, int, int]  # left, top, right, bottom заливки — для доказу


def _is_dark(px: tuple[int, int, int]) -> bool:
    """Піксель рамки: помітно темніший за будь-яке тло панелі."""
    return max(px[0], px[1], px[2]) <= 180


def _is_border_column(px, x: int, band: list[int]) -> bool:
    """Чи колонка `x` — вертикальна РАМКА смуги (а не літера підпису).

    Рамка йде на всю висоту смуги, літера — лише на частину. Тому дивимось не
    на один піксель, а на частку темних рядків у смузі: рамка дає майже всі,
    підпис — меншість.
    """
    if len(band) < 3:
        return False
    dark = sum(1 for y in band if _is_dark(px[x, y]))
    return dark >= len(band) * BORDER_DARK_SHARE


def find_progress_bar(image: Image.Image) -> Optional[ProgressBar]:
    """Знайти смугу прогресу й порахувати відсоток. None — якщо не впевнені.

    Позиція смуги НЕ зашита: інші верстати можуть мати інший макет і
    роздільність, тому вона щоразу шукається за формою й кольором.
    """
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width < 80 or height < 60:
        return None

    top = int(height * BOTTOM_BAND)
    px = rgb.load()

    # 1) Найдовший СУЦІЛЬНИЙ пробіг синього в нижній частині кадру — це і є
    #    заливка смуги. Саме пробіг, а не «розмах синього в рядку»: розмах
    #    захоплював ще й синій напис «100%» ЛІВОРУЧ від смуги та розкидані сині
    #    цифри таблиці інструментів, і детектор міряв зовсім не ту область
    #    (бойовий випадок 03.09.26 — «смуга» шириною 741px через пів екрана).
    runs: list[tuple[int, int, int, int]] = []  # (довжина, y, x0, x1)
    for y in range(top, height):
        run_start: Optional[int] = None
        for x in range(width + 1):
            blue = x < width and _is_blue(px[x, y])
            if blue and run_start is None:
                run_start = x
            elif not blue and run_start is not None:
                length = x - run_start
                if length >= MIN_FILL_WIDTH:
                    runs.append((length, y, run_start, x - 1))
                run_start = None
    if not runs:
        return None

    # 2) Кандидати — за довжиною, але беремо ПЕРШИЙ, що має форму смуги
    #    (потрібна висота). Найдовший пробіг у кадрі — панель задач Windows
    #    заввишки 2px; відкидати лише її й здаватись не можна, інакше справжня
    #    смуга нижче так і не розглядається (бойовий випадок 03.09.26).
    def band_of(left: int, y_seed: int) -> list[int]:
        def in_band(y: int) -> bool:
            return 0 <= y < height and _is_blue(px[left, y])

        y_top = y_seed
        while in_band(y_top - 1):
            y_top -= 1
        y_bot = y_seed
        while in_band(y_bot + 1):
            y_bot += 1
        return list(range(y_top, y_bot + 1))

    band: list[int] = []
    left = fill_right = 0
    for _, y_seed, x0, x1 in sorted(runs, key=lambda r: -r[0])[:MAX_BAR_CANDIDATES]:
        candidate = band_of(x0, y_seed)
        if MIN_BAR_HEIGHT <= len(candidate) <= MAX_BAR_HEIGHT:
            band, left, fill_right = candidate, x0, x1
            break
    if not band:
        return None

    # 3) ОДИН прохід зліва направо від початку смуги. Підпис («100 %») може
    #    стояти і на заливці, і на порожній частині — його літери просто
    #    пропускаємо. Спиняємось на РАМЦІ смуги: вона темна на всю висоту,
    #    а літера — лише на частину (без цього скан їхав далі по білому тлу
    #    панелі й роздував контейнер: повна смуга читалась як 85%).
    y_mid = band[len(band) // 2]
    last_blue = fill_right
    last_white = fill_right
    gap = 0
    for x in range(fill_right + 1, width):
        if _is_border_column(px, x, band):
            break
        p = px[x, y_mid]
        if _is_blue(p):
            last_blue = x
            gap = 0
        elif _is_unfilled(p):
            last_white = x
            gap = 0
        else:
            gap += 1
            if gap > MAX_LABEL_GAP:
                break

    fill = last_blue - left + 1
    # Щілина в 1-2 пікселі між заливкою й рамкою — згладжування краю, а не
    # залишок: на смузі ~128px піксель ≈ 0.8%, тобто нижче її роздільності.
    right_edge = max(last_blue, last_white)
    if right_edge - last_blue <= EDGE_SLACK:
        right_edge = last_blue
    container = right_edge - left + 1
    if container < MIN_CONTAINER_WIDTH:
        return None

    percent = round(fill * 100 / container)
    if percent < 0 or percent > 100:
        return None
    return ProgressBar(
        percent=percent, fill_width=fill, container_width=container,
        box=(left, band[0], last_white + 1, band[-1] + 1),
    )


def read_progress_percent(image: Image.Image) -> Optional[int]:
    """Відсоток виконання програми або None. Тонка обгортка для сервісу."""
    bar = find_progress_bar(image)
    return bar.percent if bar else None


# ── Ім'я .iso-програми із заголовка вікна RemiCORE ──────────────────────────
# Реальний заголовок (кадр 02.09.26):
#   `Remote - zr18_18-Monolith-A3-x62_2026-09-02_23-04-33.iso`
# У ньому дата+час — той самий ідентифікатор, що оператор вписує як Sum3D ID
# (хвіст `HH-MM-SS`). Це і є ключ, який зв'язує верстат із рядком черги.
#
# Читаємо з ТЕКСТУ заголовка (агент бере його з Windows), а не з картинки:
# здогадки тут неприпустимі — або точне ім'я, або нічого.
# Ім'я файлу — без пробілів і роздільників шляху, тож префікс вікна
# («Remote - ») у нього не залипає.
_ISO_RE = re.compile(
    r"(?P<program>[^\s\\/]*(?P<date>\d{4}-\d{2}-\d{2})[_-](?P<time>\d{2}-\d{2}-\d{2})[^\s\\/]*\.iso)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MillingProgram:
    iso_name: str      # повне ім'я файлу програми
    sum3d_id: str      # хвіст HH-MM-SS — ключ до рядка черги
    date: str          # YYYY-MM-DD з імені


def parse_iso_title(title: str) -> Optional[MillingProgram]:
    """Заголовок вікна → програма, що фрезерується. None, якщо це не воно."""
    if not title:
        return None
    m = _ISO_RE.search(title)
    if not m:
        return None
    return MillingProgram(
        iso_name=m.group("program").strip(),
        sum3d_id=m.group("time"),
        date=m.group("date"),
    )


def pick_milling_program(titles) -> Optional[MillingProgram]:
    """Обрати програму серед УСІХ заголовків вікон верстата.

    Агент віддає всі видимі вікна, бо вгадувати «те саме» вікно на його боці —
    зайва здогадка. Тут беремо перший заголовок, що виглядає як `.iso`-програма;
    якщо таких кілька (відкрито два вікна RemiCORE), беремо перший — але лише
    коли всі вони кажуть про ОДНУ програму, інакше нічого: два різні кандидати
    означають, що ми не знаємо, який фрезерується (принцип «краще нічого»).
    """
    found = [p for p in (parse_iso_title(t) for t in (titles or [])) if p]
    if not found:
        return None
    first = found[0]
    if any(p.sum3d_id != first.sum3d_id or p.date != first.date for p in found):
        return None
    return first
