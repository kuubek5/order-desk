"""Яка програма фрезерується — з екрана JOBS верстатів нового покоління.

**Навіщо.** Зв'язка «верстат ↔ рядок черги» тримається на Sum3D ID у назві
програми (`…_2026-09-04_12-57-22.iso`, хвіст `HH-MM-SS`). На RemiCORE агент
бере її із ЗАГОЛОВКА вікна. Софт нового покоління (CORiTEC 150i PRO,
250i PRO+, пласкі екрани) у заголовок назву не пише — вона стоїть лише НА
ЕКРАНІ, у списку QUEUE поруч із кружком ▶ (скарга власника 10.09.26: «нового
покоління станки не показують, хто зараз фрезерується»). Тому тут читаємо
саме той рядок.

**Правило те саме: або точно, або нічого.** Хибний Sum3D ID підсвітив би в
черзі чужу роботу як «фрезерується» — гірше, ніж не підсвітити нічого. Тому
прочитане проходить три незалежні перевірки, і кожна сама по собі здатна
сказати «ні»:

  1. кожна цифра впізнана з великим відривом від найближчої іншої цифри;
  2. хвіст назви складається рівно в `РРРР-ММ-ДД ГГ-ХХ-СС .ISO` з допустимими
     числами (місяць 1–12, хвилина < 60 …);
  3. такий Sum3D ID справді є в робочій черзі, у роботи з днем поруч із
     датою з назви програми — це перевіряє викликач (у нього база,
     `machines._program_from_screen`), і лише тоді прив'язка ставиться.

**Чому не «біт-у-біт», як у SISMA.** Виміряно на бойових кадрах 04.09.26: той
самий кадр дає ті самі пікселі (кадр до кадру стабільний), але ТА САМА цифра
в різних місцях рядка малюється трохи інакше — софт ставить символи з
точністю до частки пікселя («2» мала три варіанти, «0» — чотири). Звірка
біт-у-біт тут відкинула б більшість цифр. Натомість символ зводиться до
спільного розміру, і порівнюється відстань до еталонів.

**Запас тут менший, ніж здається, і це навмисна пересторога.** Спершу тут
стояло, що варіанти однієї цифри лежать у межах ~22, а різні цифри не ближче
~40 (найтісніша пара 6/5), тож пороги стоять посеред широкого проміжку. Набір
еталонів відтоді виріс, і виміряно 13.09.26 це вже не так: найтісніша пара
різних цифр — **3/8 на 21.2**, а розкид УСЕРЕДИНІ цифри доходить до 42 («1»,
через субпіксельний зсув тонкого штриха) і 47 («4»). Тобто проміжки
перекриваються, і від ХИБНОЇ цифри рятує вже не запас відстані, а вимога
ВІДРИВУ (`MIN_MARGIN`).

Практичний наслідок, який варто знати, перш ніж крутити пороги: коли для
конкретного накреслення потрібного варіанта ще немає, правильна цифра стоїть
далеко (~15), чужа поруч (~27), і відрив не добирає буквально одиниць. Лікує це
НЕ послаблення порогів (воно одразу пускає 3 замість 8), а додавання того
самого варіанта — `scripts/newgen_glyphs.py learn`. Саме тому кадр, на якому
читач спіткнувся, разом із вирізом рядка назви в рідному масштабі лягає у
скриньку невідомих екранів (`name_row_crop`): без нього вчити нема на чому.

Той самий прийом дає ще й перенос між верстатами: у 150i шрифт більший
(висота 22 проти 18 у 250i), але після зведення до спільного розміру цифри
150i впізнаються еталонами 250i — перевірено на всіх 20 цифрах кадру 150i.
Тому еталони спільні, а цифри, яких на одному верстаті ще не траплялось,
береться з іншого.

**Повна назва (12.10.26).** Табло цеху показує біля диска весь рядок програми,
тож читається й початок назви до дати (`1_14-MONOLITH-A3-X89`) — літери,
цифри, `-`, `_` і перенос на другий рядок. Правило те саме, ще суворіше: один
непевний символ, незнайома позначка під рядком чи перенос не після «-» — і
початку немає зовсім, назва лишається датою й часом, як досі. На Sum3D ID і
прив'язку до черги початок назви не впливає. Подробиці — у розділі «Повна
назва» нижче.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from app.machine_ocr import MillingProgram, _cache_only_success
from app.runtime import resource_path

logger = logging.getLogger(__name__)

NEWGEN_GLYPHS_PATH = "app/data/newgen_glyphs.json"

# Чорнило тексту: білий на тлі картки (66,66,66). Облямівки згладжування
# кольорові (ClearType) — у маску їх не беремо, лише нейтральне світле ядро.
INK_MIN = 150
INK_MAX_SPREAD = 40
# Кружок ▶ поточної роботи — насичений колір (синій на 250i, жовтий на 150i).
ICON_SPREAD = 90
# Де шукати кружок — частками кадру: ліва панель QUEUE.
ICON_ZONE = (0.055, 0.18, 0.125, 0.90)   # x0, y0, x1, y1
# Права межа рядка назви: середина кадру, далі вже інша картка.
NAME_RIGHT = 0.49
# Назва й тривалість («0H 22MIN») стоять в одному рядку. У назві пробілів
# немає, тож проміжок ширший за цей — уже тривалість.
#
# Поріг узято з виміру всіх бойових кадрів обох поколінь (14.09.26), а не на
# око: найширший проміжок УСЕРЕДИНІ назви — 16 px, найвужчий проміжок ДО
# тривалості — 39 px. Годиться будь-що з 17..38; 26 стоїть посередині.
#
# Було 40 — і на 150i це різало впритул повз: там назва довша, тривалість
# підходить ближче, і проміжок склав рівно 39. Одного пікселя забракло, щоб
# відрізати «00H 38MIN», ті символи доклеювались до назви, і хвіст бралися
# читати з тривалості. Помилка виглядала як «не впізнано цифру» з дистанцією
# 85 — тобто як брак еталона, хоча еталони були ні до чого. На 250i та сама
# межа проходила з запасом (проміжок 216..233), тому ламалось лише 150i.
BLOCK_GAP = 26
# Кружок має бути помітно великим: 2 % висоти кадру (виміряно 38–55 px на
# 1200). Дрібні кольорові плями — іконки інструментів, облямівки.
ICON_MIN_HEIGHT = 0.02
# … але й не панеллю: на RemiCORE у цій зоні стоїть висока синя бічна смуга
# (457 px), і без стелі вона ставала «кружком», а зона тексту — цілим кадром.
# Кружок — майже квадрат, тож ще й пропорції.
ICON_MAX_HEIGHT = 0.07
ICON_ASPECT = (0.7, 1.4)

# Спільний розмір, до якого зводиться цифра перед порівнянням.
NORM_SIZE = (12, 16)
# Відстань до найближчого еталона — не більша за цю (варіанти однієї цифри
# виміряно до ~22) …
MAX_DISTANCE = 28.0
# … і найближча ІНША цифра — далі щонайменше на стільки (найтісніша бойова
# пара 6/5: 21 проти 40, тобто відрив 19).
MIN_MARGIN = 12.0

# Хвіст назви після того, як `_` зникає (підкреслення лежить нижче базової
# лінії, а символи ріжемо між верхом рядка й базовою лінією):
# `2026-09-04_12-57-22.ISO` → `2026-09-0412-57-22.ISO`.
TAIL = "####-##-####-##-##.I??"


@dataclass(frozen=True)
class _Glyph:
    bits: np.ndarray    # bool, рядки × колонки
    top: int            # відступ від верху рядка
    cap: int            # висота рядка від верху до базової лінії
    # Де символ стоїть: ліва колонка в рядку назви й номер рядка (назва
    # переноситься). Хвостові дата й час цього не потребують; потрібно це
    # повній назві — щоб поставити `_` між правильними символами.
    left: int = 0
    line: int = 0


@dataclass(frozen=True)
class _Mark:
    """Чорнило НИЖЧЕ базової лінії рядка. У назві програми це лише `_`.

    `underscore=False` — там є щось інше (хвіст Q, кома, уламок рамки, рядок,
    чию базову лінію визначено хибно). Повна назва тоді не читається зовсім:
    вгадувати, що це було, не беремося."""

    line: int
    left: int
    right: int
    underscore: bool


@dataclass(frozen=True)
class _Layout:
    glyphs: list[_Glyph]
    marks: list[_Mark]


@_cache_only_success
def load_newgen_glyphs() -> dict[str, list[np.ndarray]]:
    """Еталони цифр: символ → варіанти, уже зведені до NORM_SIZE.

    Немає файлу — порожньо, і читач просто мовчить (як і в інших читачів
    екрана: «ще не навчено» — це не поломка)."""
    path = Path(resource_path(NEWGEN_GLYPHS_PATH))
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("newgen_glyphs.json не прочитано — назву програми з екрана не читаємо")
        return {}
    out: dict[str, list[np.ndarray]] = {}
    for char, variants in (raw.get("digits") or {}).items():
        out[char] = [_normalise(np.array([[c == "1" for c in row] for row in rows], dtype=bool))
                     for rows in variants]
    return out


def _normalise(bits: np.ndarray) -> np.ndarray:
    image = Image.fromarray(bits.astype(np.uint8) * 255).resize(NORM_SIZE, Image.BILINEAR)
    return np.asarray(image, dtype=np.float32) / 255.0


def _runs(flags) -> list[tuple[int, int]]:
    """Суцільні відрізки True: [(початок, кінець), …]."""
    flags = np.asarray(flags, dtype=bool)
    if not flags.any():
        return []
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist()))


def _find_icon(image: Image.Image) -> Optional[tuple[int, int, int]]:
    """Кружок ▶ поточної роботи: (правий край, верх, низ) у координатах кадру.

    Лише СУЦІЛЬНА кольорова пляма: піксель і сусіди на відстані 2 px мусять
    бути насиченими. Тонкі кольорові облямівки тексту цієї умови не проходять
    (саме на них перший підхід і спіткнувся — «кружок» розтягувався на весь
    текст). Два кружки — не знаємо, котра програма поточна, мовчимо.

    Ріжемо зону ДО перетворення в масив: читач запускається на кожному кадрі
    кожного верстата без програми в заголовку, а опитування йде по черзі —
    переганяти 1920×1200 заради смужки в 7 % ширини означало б старити решту
    верстатів. Виміряно на фікстурах: було до 43 мс на кадр; з обрізанням і
    стелею розміру кружка — ~3 мс на чужих екранах і 13–19 мс на самому JOBS."""
    width, height = image.size
    x0, y0 = int(width * ICON_ZONE[0]), int(height * ICON_ZONE[1])
    x1, y1 = int(width * ICON_ZONE[2]), int(height * ICON_ZONE[3])
    zone = np.asarray(image.crop((x0, y0, x1, y1)).convert("RGB")).astype(np.int16)
    sat = (zone.max(axis=2) - zone.min(axis=2)) >= ICON_SPREAD
    solid = sat.copy()
    for dy in (-2, 0, 2):
        for dx in (-2, 0, 2):
            solid &= np.roll(np.roll(sat, dy, axis=0), dx, axis=1)
    icons = []
    for a, b in _runs(solid.any(axis=1)):
        if not (height * ICON_MIN_HEIGHT <= b - a <= height * ICON_MAX_HEIGHT):
            continue
        cols = np.flatnonzero(solid[a:b].any(axis=0))
        aspect = (cols[-1] - cols[0] + 1) / (b - a)
        if ICON_ASPECT[0] <= aspect <= ICON_ASPECT[1]:
            icons.append((x0 + int(cols[-1]) + 1, y0 + a, y0 + b))
    return icons[0] if len(icons) == 1 else None


def _name_box(image: Image.Image) -> Optional[tuple[int, int, int, int]]:
    """Прямокутник рядка назви поруч із кружком ▶, або None."""
    width, height = image.size
    icon = _find_icon(image)
    if icon is None:
        return None
    icon_right, icon_top, icon_bottom = icon
    icon_h = icon_bottom - icon_top
    rx0, rx1 = icon_right + 4, int(width * NAME_RIGHT)
    ry0, ry1 = max(0, icon_top - icon_h), min(height, icon_bottom + icon_h)
    if rx1 - rx0 < 50:
        return None
    return rx0, ry0, rx1, ry1


def name_row_crop(image: Image.Image) -> Optional[Image.Image]:
    """Той самий рядок назви, але картинкою й у РІДНОМУ масштабі.

    Навіщо. Коли цифра не впізналась, учити її треба саме на цих пікселях. Кадр
    цілком по мережі не дістати (скринька невідомих зберігає зменшену копію
    640×400, і MCP віддає таку саму), а на 640 рядок назви не сегментується
    взагалі — жодного гліфа, перевірено 13.09.26. Смужка ж важить копійки й
    доїжджає цілою. Той самий прийом, що з вирізами зон печей, і та сама
    причина: «зменшена копія растрового шрифту перетворює навчання на
    вгадування».

    Геометрія береться тією САМОЮ функцією, що й для читання, — інакше вчили б
    одне, а звіряли інше."""
    box = _name_box(image)
    return image.crop(box).convert("RGB") if box is not None else None


#: Розклад кадру, уже розрізаного в межах ОДНОГО виклику читача (див.
#: `read_newgen_program_variants`). Лише на час виклику й лише свого потоку:
#: тотожність `is` того самого об'єкта кадру в одному виклику — гарантія,
#: що це ті самі пікселі.
_layout_memo = threading.local()


def _name_layout(image: Image.Image) -> Optional[_Layout]:
    """Символи назви поточної програми + чорнило під базовою лінією (`_`)."""
    memo = getattr(_layout_memo, "pair", None)
    if memo is not None and memo[0] is image:
        return memo[1]
    box = _name_box(image)
    if box is None:
        return None
    return _layout_in_zone(image.crop(box))


def _name_glyphs(image: Image.Image) -> Optional[list[_Glyph]]:
    """Символи назви поточної програми, рядок за рядком, зліва направо."""
    layout = _name_layout(image)
    return None if layout is None else layout.glyphs


def glyphs_from_name_row(image: Image.Image) -> Optional[list[_Glyph]]:
    """Ті самі символи, але з уже ВИРІЗАНОГО рядка назви (`name_row_crop`).

    Навіщо окремий вхід. Кружок ▶ шукається часткою ШИРИНИ КАДРУ, тож у вирізі,
    який починається одразу за ним, його немає — і `_name_glyphs` на такому
    вирізі чесно повертала порожньо. Через це виріз, покладений у скриньку саме
    для донавчання, навчити нічого не міг: подивитись на нього було можна, а
    згодувати `learn` — ні. Тут зона вже відома (це весь виріз), тож шукати
    нічого не треба, а далі йде ТА САМА сегментація, що й при читанні."""
    return _glyphs_in_zone(image)


def _glyphs_in_zone(zone_image: Image.Image) -> Optional[list[_Glyph]]:
    """Сегментація символів у вже вибраній зоні рядка назви."""
    layout = _layout_in_zone(zone_image)
    return None if layout is None else layout.glyphs


def _layout_in_zone(zone_image: Image.Image) -> Optional[_Layout]:
    """Сегментація зони рядка назви: символи й чорнило під базовою лінією.

    Символи — рівно ті, що завжди (хвіст дата+час читається з них без змін);
    позначки під базовою лінією — додатково, для повної назви."""
    zone = np.asarray(zone_image.convert("RGB")).astype(np.int16)
    ink = (zone.min(axis=2) >= INK_MIN) & ((zone.max(axis=2) - zone.min(axis=2)) <= INK_MAX_SPREAD)

    blocks = _runs(ink.any(axis=0))
    if not blocks:
        return None
    nx0, nx1 = blocks[0]
    for a, b in blocks[1:]:
        if a - nx1 > BLOCK_GAP:
            break
        nx1 = b
    name = ink[:, nx0:nx1]

    glyphs: list[_Glyph] = []
    marks: list[_Mark] = []
    line_no = 0
    for top, bottom in _runs(name.any(axis=1)):
        line = name[top:bottom]
        # Тривалість ПІД назвою (250i-Tolik, 02.10.26): назва
        # «PMMA16_16-A2-X184_2026-10-02_03-02-10.ISO» довга, і «2H 08MIN»
        # переноситься на другий рядок, вирівняний праворуч. BLOCK_GAP її не
        # відсікає (вона під назвою, не збоку), і хвіст, який береться з КІНЦЯ,
        # читався з «…ISO2H08MIN» — «цифра №1» виходила літерою I. Продовження
        # самої назви починалося б від лівого краю; рядок, що стартує далі за
        # три висоти символу від нього, — уже не назва.
        if glyphs:
            start = np.flatnonzero(line.any(axis=0))
            if start.size and start[0] > 3 * (bottom - top):
                continue
        # Базова лінія — останній рядок пікселів, де чорнило стоїть багатьма
        # окремими відрізками (низи всіх символів). Нижче лишається лише `_`.
        pieces = [len(_runs(row)) for row in line]
        # Порожня смуга (жодного рядка з чорнилом) — `max(pieces)` тут падав
        # ValueError і валив читання ВСЬОГО кадру. Guard нижче (`if not bases`)
        # ставили саме проти цього, але на ДРУГИЙ max; а падає ПЕРШИЙ,
        # `max(pieces)`, — до нього черга не доходила лише на dev, де таких
        # смуг не траплялось. На проді — 398 падінь за 2 год (лог 27.09.26,
        # «max() arg is an empty sequence» із `_program_from_screen`): кожне
        # будувало traceback і крутило numpy намарно. Порожня смуга гліфів не
        # дає — пропускаємо, решта рядка читається далі.
        if not pieces:
            continue
        busiest = max(pieces)
        bases = [i for i, n in enumerate(pieces) if n >= max(4, 0.3 * busiest)]
        if not bases:
            # Смуга, де ЖОДЕН рядок пікселів не має чотирьох окремих відрізків,
            # рядком назви бути не може (там суцільні цифри й дефіси) — це
            # уламок: підкреслення, облямівка, шматок рамки. Базової лінії в
            # ньому немає. Пропускаємо смугу: решта рядка читається далі, а
            # якщо не прочитається — відмова буде чесною, з номером символу.
            continue
        base = max(bases)
        cap_band = line[: base + 1]
        for a, b in _runs(cap_band.any(axis=0)):
            column = cap_band[:, a:b]
            rows = np.flatnonzero(column.any(axis=1))
            glyphs.append(_Glyph(bits=column[rows[0]: rows[-1] + 1], top=int(rows[0]), cap=base + 1,
                                 left=int(a), line=line_no))
        below = line[base + 1:]
        for a, b in _runs(below.any(axis=0)):
            marks.append(_Mark(line=line_no, left=int(a), right=int(b),
                               underscore=_is_underscore(below[:, a:b], base + 1)))
        line_no += 1
    return _Layout(glyphs=glyphs, marks=marks)


# ── Підкреслення `_` ────────────────────────────────────────────────────────
# Виміряно на всіх бойових кадрах обох поколінь (12.10.26): суцільний
# прямокутник одразу під базовою лінією, 2–3 px заввишки, 13–14 px завширшки
# при висоті рядка 18 (250i) і 16–17 px при 22 (150i), тобто 0.72–0.78 висоти
# рядка. Злиплі `__` були б удвічі ширші — не пройдуть, і повна назва тоді
# не читається (скільки їх там, не вгадуємо).
UNDERSCORE_WIDTH = (0.6, 0.9)
UNDERSCORE_MAX_HEIGHT = 0.2
UNDERSCORE_MAX_DROP = 0.1


def _is_underscore(piece: np.ndarray, cap: int) -> bool:
    rows = np.flatnonzero(piece.any(axis=1))
    if rows.size == 0:
        return False
    body = piece[rows[0]: rows[-1] + 1]
    h, w = body.shape
    return bool(
        rows[0] <= max(1, cap * UNDERSCORE_MAX_DROP)
        and h <= max(2, cap * UNDERSCORE_MAX_HEIGHT)
        and UNDERSCORE_WIDTH[0] * cap <= w <= UNDERSCORE_WIDTH[1] * cap
        and body.mean() >= 0.9
    )


def _shape_class(glyph: _Glyph) -> Optional[str]:
    """`-`, `.` чи `I` — за формою, без еталонів.

    Звести їх до спільного розміру, як цифри, не можна: риска й крапка тоді
    стають однаковими суцільними прямокутниками. Натомість у них дуже
    характерна геометрія відносно висоти рядка."""
    h, w = glyph.bits.shape
    cap = glyph.cap
    fill = glyph.bits.mean()
    if h <= cap * 0.25:
        middle = glyph.top + h / 2
        if fill >= 0.8 and w >= 1.5 * h and 0.3 * cap <= middle <= 0.7 * cap:
            return "-"
        # Крапка кругла: у 150i вона 4×4 без кутів, тобто заповнення 0.75
        # (бойовий кадр 10.09.26; у фікстурі було 0.81 — впритул до старого
        # спільного з рискою порогу 0.8, і перший же кадр із цеху його
        # переступив). Помилки тут не буде: позицію крапки задає TAIL, а дату
        # й час навколо неї все одно перевіряють цифри.
        if fill >= 0.65 and w <= 2 * h and glyph.top + h >= cap * 0.85:
            return "."
    if h >= cap * 0.9 and w <= cap * 0.25 and fill >= 0.8:
        return "I"
    return None


def _ranked(glyph: _Glyph, templates: dict[str, list[np.ndarray]]) -> list[tuple[str, float]]:
    """Цифри за відстанню до найближчого свого еталона, найближча перша."""
    probe = _normalise(glyph.bits)
    best = {
        char: min(float(np.abs(probe - v).sum()) for v in variants)
        for char, variants in templates.items()
    }
    return sorted(best.items(), key=lambda kv: kv[1])


#: Скільки цифр хвоста можна доуточнити ЧЕРГОЮ, коли шрифт не дав відриву.
#: Дві — стеля свідомо: на трьох перебір дає вже до восьми варіантів, і
#: «рівно один збігся з чергою» перестає бути рідкісною подією.
UNSURE_LIMIT = 2

#: І стеля на сам перебір. Непевна цифра зазвичай має двох претендентів (на
#: наявних еталонах більше двох не буває жодного разу — перевірено), але
#: розмазаний гліф може опинитись у межах MAX_DISTANCE від пів десятка цифр.
#: Кожне прочитання коштує запиту в базу НА КОЖНОМУ тіку опитування, доки
#: екран не прочитається, тож нехай краще мовчить, ніж перебирає.
VARIANTS_LIMIT = 4


@dataclass(frozen=True)
class _Pick:
    """Що вийшло з одного символа хвоста.

    `char` — впевнений символ (правила ті самі: ≤MAX_DISTANCE і відрив
    ≥MIN_MARGIN). `options` — усе, що лишилось правдоподібним: з них
    складають варіанти прочитання, коли шрифт не вирішив сам, а вирішити
    може черга. Порожні `options` означають «це взагалі не цифра» — тоді
    не допоможе й черга.
    """

    char: "Optional[str]"
    options: "tuple[str, ...]"


def _digit_pick(glyph: "_Glyph", templates: "dict[str, list[np.ndarray]]") -> "_Pick":
    """Цифра — впевнено, правдоподібно або ніяк."""
    h, w = glyph.bits.shape
    if h < glyph.cap * 0.9 or not templates:
        return _Pick(None, ())
    ranked = _ranked(glyph, templates)
    char, distance = ranked[0]
    plausible = tuple(c for c, d in ranked if d <= MAX_DISTANCE and c.isdigit())
    if distance > MAX_DISTANCE:
        return _Pick(None, ())
    if len(ranked) > 1 and ranked[1][1] - distance < MIN_MARGIN:
        return _Pick(None, plausible)
    return _Pick(char, plausible or (char,))


def _tail_picks(
    image: Image.Image, templates: "dict[str, list[np.ndarray]]"
) -> "tuple[Optional[list[_Pick]], Optional[str]]":
    """Хвіст назви посимвольно. `None, None` — тут нема чого читати."""
    return _tail_picks_in(_name_glyphs(image), templates)


def _tail_picks_in(
    glyphs: "Optional[list[_Glyph]]", templates: "dict[str, list[np.ndarray]]"
) -> "tuple[Optional[list[_Pick]], Optional[str]]":
    """Те саме над уже розрізаними символами (кадр ріжеться один раз)."""
    # Короткий рядок біля кружка — не назва програми (SUMMARY теж має кружок
    # і кілька слів поруч): читати нема чого, це не відмова.
    if glyphs is None or len(glyphs) < len(TAIL):
        return None, None
    picks: "list[_Pick]" = []
    for index, (glyph, want) in enumerate(zip(glyphs[-len(TAIL):], TAIL)):
        if want == "?":
            picks.append(_Pick("?", ("?",)))
            continue
        if want == "#":
            pick = _digit_pick(glyph, templates)
            if pick.char is None and not pick.options:
                ranked = _ranked(glyph, templates)
                near = ", ".join(f"{c}={d:.0f}" for c, d in ranked[:2])
                return None, (
                    f"цифру №{index + 1} хвоста не впізнано ({near}; треба ≤{MAX_DISTANCE:.0f} "
                    f"і відрив ≥{MIN_MARGIN:.0f}) — донавчити: scripts/newgen_glyphs.py learn"
                )
            picks.append(pick)
            continue
        got = _shape_class(glyph)
        if got != want:
            h, w = glyph.bits.shape
            return None, (
                f"символ №{index + 1} хвоста мав бути «{want}», а форма {w}×{h}, "
                f"заповнення {glyph.bits.mean():.2f}"
            )
        picks.append(_Pick(got, (got,)))
    return picks, None


# ── Повна назва: початок до дати (12.10.26) ─────────────────────────────────
#
# Навіщо. Табло цеху показує біля диска ВЕСЬ рядок програми
# (`1_14-MONOLITH-A3-X89_2026-10-12_09-42-30`): за ним людина впізнає диск,
# не шукаючи Sum3D ID. RemiCORE дає його заголовком вікна; тут — лише з
# екрана, а досі читались тільки дата й час.
#
# Правило те саме: або точно, або нічого. Початок назви або прочитано ЦІЛКОМ —
# кожен символ, кожне `_`, кожен перенос, — або його немає зовсім, і назва
# лишається такою, як була (`2026-10-12_09-42-30.iso`). Хвіст (дата, час,
# Sum3D ID, прив'язка до черги) від цього не залежить ніяк.
#
# Чому не тим самим зведенням, що цифри хвоста. Цифри зводяться до 12×16 і
# втрачають пропорцію — для них це байдуже, а для літер ні: «O» і «0», «B» і
# «8» відрізняються саме шириною. Тут символ зводиться до спільної ВИСОТИ
# зі збереженою пропорцією й стає посередині полотна 32×24.
#
# Пороги — з виміру на 17 бойових рядках (12.10.26, 578 символів на всю
# висоту, кожен звірено з еталонами ЛИШЕ з інших кадрів):
#   - найближчі РІЗНІ символи — 3/8 на 25.3, далі 5/6 38, C/O 39, P/R 42;
#     тобто символ, якого ще не вчили, на відстані ≤20 від чужого еталона не
#     опинився жодного разу — і NAME_MAX_DISTANCE стоїть нижче;
#   - той самий символ з іншого кадру: медіана 0 (софт малює його тими самими
#     пікселями), 75 % ≤9, 90 % ≤27 — решта чесно відмовляє, доки накреслення
#     не довчено (`scripts/newgen_glyphs.py learn`).
NAME_NORM = (32, 24)            # (ширина, висота)
NAME_MAX_DISTANCE = 20.0
NAME_MIN_MARGIN = 10.0
# Одинарна риска — 0.33–0.41 висоти рядка; злиплі «--» були б ≥0.7.
DASH_MAX_WIDTH = 0.55
# Пробіл. Його в назві не буває (ім'я файлу Sum3D; на тому самому стоять і
# `_ISO_RE`, і BLOCK_GAP), але він не дав би символа — тихо злив би два
# слова. Тому проміжок між сусідами без `_` між ними обмежено: виміряно не
# більше 0.28 висоти рядка, а після «1» (у цифр однакова ширина, у «1» великий
# правий відступ) і «.» — до 0.50. Пробіл Roboto ≈0.35 висоти плюс відступи.
NAME_GAP = 0.34
NAME_GAP_WIDE = 0.65


class _NameTemplates(dict[str, np.ndarray]):
    """Символ → стос варіантів NAME_NORM, плюс `exact`: точна бітмапа → символ.

    Навіщо `exact`. Софт малює той самий символ на тому самому місці тими
    самими пікселями (медіана відстані до свого еталона — 0), тож майже кожен
    символ назви — точна копія вже вивченого. Для такої копії повне порівняння
    дало б відомий наперед результат: відстань 0 до свого символу й рівно ту
    відстань до найближчого ЧУЖОГО, що має сам еталон. Тому в `exact` лише ті
    еталони, чий найближчий чужий стоїть не ближче NAME_MIN_MARGIN, — для них
    відповідь та сама, а коштує один пошук у словнику замість ~300 порівнянь
    (виміряно: −6 мс на кадр JOBS)."""

    exact: "dict[tuple, str]"


def _bits_key(bits: np.ndarray) -> tuple:
    return (bits.shape, np.asarray(bits, dtype=bool).tobytes())


@_cache_only_success
def load_newgen_name_glyphs() -> "dict[str, np.ndarray]":
    """Еталони для початку назви: символ → стос варіантів NAME_NORM.

    Цифри — ті самі бітмапи, що й для хвоста (`digits`), плюс `name_digits`:
    накреслення цифр, вивчені САМЕ для назви (`learn --letters-only`). Окремий
    розділ — щоб навчання назви не змінювало еталонів, якими читаються дата й
    час (12.10.26: у цеху всі три верстати спотикались на цифрах початку
    назви — «7» у «7_16», «4» у «X45», — бо в набір хвоста їх ніхто не вчив).
    Літери — `letters`.
    Немає літер — початок назви не читається (а хвіст читається як завжди)."""
    path = Path(resource_path(NEWGEN_GLYPHS_PATH))
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("newgen_glyphs.json не прочитано — повну назву програми з екрана не читаємо")
        return {}
    by_char: "dict[str, list[np.ndarray]]" = {}
    for section in ("digits", "name_digits", "letters"):
        for char, variants in (raw.get(section) or {}).items():
            by_char.setdefault(char, []).extend(
                np.array([[c == "1" for c in row] for row in rows], dtype=bool) for rows in variants
            )
    out = _NameTemplates()
    entries: "list[tuple[str, np.ndarray]]" = []
    for char, bitmaps in by_char.items():
        if bitmaps:
            out[char] = np.stack([_name_norm(b) for b in bitmaps])
            entries.extend((char, b) for b in bitmaps)
    out.exact = {}
    if entries:
        flat = np.concatenate([out[char] for char in out]).reshape(len(entries), -1)
        labels = np.array([char for char in out for _ in range(len(out[char]))])
        for index, (char, bits) in enumerate(entries):
            others = labels != char
            nearest_other = float(np.abs(flat[others] - flat[index]).sum(axis=1).min()) if others.any() else np.inf
            if nearest_other >= NAME_MIN_MARGIN:
                out.exact[_bits_key(bits)] = char
    return out


def _name_norm(bits: np.ndarray) -> np.ndarray:
    """Символ на полотні NAME_NORM: спільна висота, власна пропорція."""
    h, w = bits.shape
    width = max(1, min(NAME_NORM[0], round(w * NAME_NORM[1] / h)))
    scaled = Image.fromarray(bits.astype(np.uint8) * 255).resize((width, NAME_NORM[1]), Image.Resampling.BILINEAR)
    canvas = np.zeros((NAME_NORM[1], NAME_NORM[0]), dtype=np.float32)
    x0 = (NAME_NORM[0] - width) // 2
    canvas[:, x0: x0 + width] = np.asarray(scaled, dtype=np.float32) / 255.0
    return canvas


def _name_char(glyph: _Glyph, templates: "dict[str, np.ndarray]") -> "tuple[Optional[str], str]":
    """Один символ початку назви — впевнено, або (None, чому)."""
    h, w = glyph.bits.shape
    shape = _shape_class(glyph)
    if shape == "-":
        if w <= DASH_MAX_WIDTH * glyph.cap:
            return "-", ""
        return None, f"риска {w} px — ширша за одну (злиплі «--»?)"
    if shape == ".":
        return ".", ""
    if h < glyph.cap * 0.9 or glyph.top > glyph.cap * 0.1:
        return None, f"символ {w}×{h} не на всю висоту рядка"
    exact = getattr(templates, "exact", None)
    if exact:
        known = exact.get(_bits_key(glyph.bits))
        if known is not None:
            return known, ""
    probe = _name_norm(glyph.bits)
    ranked = sorted(
        ((char, float(np.abs(stack - probe).sum(axis=(1, 2)).min())) for char, stack in templates.items()),
        key=lambda kv: kv[1],
    )
    if not ranked:
        return None, "еталонів немає"
    char, distance = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else ("", float("inf"))
    if distance > NAME_MAX_DISTANCE or runner_up[1] - distance < NAME_MIN_MARGIN:
        return None, (
            f"не впізнано ({char}={distance:.0f}, {runner_up[0]}={runner_up[1]:.0f}; треба "
            f"≤{NAME_MAX_DISTANCE:.0f} і відрив ≥{NAME_MIN_MARGIN:.0f})"
        )
    return char, ""


def _underscore_slots(layout: _Layout) -> "tuple[Optional[list[int]], Optional[str]]":
    """Де стоять `_`: номер символу, ПЕРЕД яким кожне (перенос — наскрізно).

    Підкреслення наприкінці рядка й на початку наступного дають той самий
    номер: це одне місце в назві."""
    glyphs = layout.glyphs
    first: "dict[int, int]" = {}
    last: "dict[int, int]" = {}
    for index, glyph in enumerate(glyphs):
        first.setdefault(glyph.line, index)
        last[glyph.line] = index + 1
    slots: "list[int]" = []
    for mark in layout.marks:
        if not mark.underscore:
            return None, f"під рядком {mark.line + 1} не підкреслення ({mark.right - mark.left} px)"
        if mark.line not in first:
            return None, "підкреслення в рядку без символів"
        centre = (mark.left + mark.right) / 2
        line_glyphs = glyphs[first[mark.line]: last[mark.line]]
        if any(g.left <= centre < g.left + g.bits.shape[1] for g in line_glyphs):
            return None, "підкреслення під символом, а не між символами"
        before = sum(1 for g in line_glyphs if g.left + g.bits.shape[1] / 2 < centre)
        slots.append(first[mark.line] + before)
    if len(set(slots)) != len(slots):
        return None, "два підкреслення в одному місці"
    return sorted(slots), None


def _name_prefix(layout: _Layout) -> "tuple[str, Optional[str]]":
    """Початок назви до `_дата` — цілком або "" з причиною.

    Що мусить зійтися, щоб початок назви взагалі був:
      1. підкреслення стоять рівно перед датою й перед часом (так Sum3D пише
         кожну назву) і ніде більше в хвості; будь-яке інше чорнило під
         базовою лінією — відмова;
      2. кожен символ до дати впізнано впевнено (`_name_char`);
      3. перенос рядка — лише після «-» (за ним софт і переносить): інакше на
         місці переносу міг стояти пробіл, і злиті слова були б вигадкою;
      4. між сусідами в рядку немає проміжку, схожого на пробіл.
    """
    glyphs = layout.glyphs
    date_at = len(glyphs) - len(TAIL)
    if date_at <= 0:
        return "", "перед датою нічого немає"
    templates = load_newgen_name_glyphs()
    if not templates:
        return "", "еталонів для назви немає"
    slots, why = _underscore_slots(layout)
    if slots is None:
        return "", why
    if [s for s in slots if s >= date_at] != [date_at, date_at + 10]:
        return "", "підкреслення не там, де їх ставить Sum3D (перед датою й перед часом)"
    under = set(slots)
    chars: "list[str]" = []
    for index in range(date_at):
        char, why_not = _name_char(glyphs[index], templates)
        if char is None:
            return "", f"символ №{index + 1} назви: {why_not}"
        chars.append(char)
    for index in range(date_at - 1):
        if index + 1 in under:
            continue
        a, b = glyphs[index], glyphs[index + 1]
        if a.line != b.line:
            if chars[index] != "-":
                return "", f"перенос рядка після «{chars[index]}», а не після «-»"
            continue
        gap = b.left - (a.left + a.bits.shape[1])
        limit = NAME_GAP_WIDE if chars[index] in "1." else NAME_GAP
        if gap > limit * a.cap:
            return "", f"проміжок {gap} px після символа №{index + 1} — схоже на пробіл"
    return "".join(("_" if i in under else "") + c for i, c in enumerate(chars)), None


def read_newgen_name_prefix_explained(image: Image.Image) -> "tuple[str, Optional[str]]":
    """Початок назви з кадру (діагностика, `check`): (текст, чому ні)."""
    layout = _name_layout(image)
    if layout is None:
        return "", "рядка ▶ з назвою на кадрі немає"
    return _name_prefix(layout)


def _program_from_text(s: str, prefix: str = "") -> "tuple[Optional[MillingProgram], Optional[str]]":
    """Зібрати програму з прочитаного хвоста, перевіривши дату й час.

    `prefix` — початок назви до дати (`1_14-MONOLITH-A3-X89`), лише коли його
    прочитано ЦІЛКОМ (`_name_prefix`); інакше порожньо, і назва, як і досі, —
    сама дата й час. На Sum3D ID і дату префікс не впливає ніяк."""
    # ####-##-## ##-##-## .I??
    year, month, day = s[0:4], s[5:7], s[8:10]
    hour, minute, second = s[10:12], s[13:15], s[16:18]
    if not (2020 <= int(year) <= 2099 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None, f"прочитано неможливу дату {year}-{month}-{day}"
    if not (int(hour) < 24 and int(minute) < 60 and int(second) < 60):
        return None, f"прочитано неможливий час {hour}-{minute}-{second}"
    date_text = f"{year}-{month}-{day}"
    time_text = f"{hour}-{minute}-{second}"
    head = f"{prefix}_" if prefix else ""
    return (
        MillingProgram(
            iso_name=f"{head}{date_text}_{time_text}.iso", sum3d_id=time_text, date=date_text
        ),
        None,
    )


def read_newgen_program_variants(image: Image.Image) -> "list[MillingProgram]":
    """Усі прочитання, які шрифт вважає правдоподібними, коли впевненості нема.

    Потрібне рівно для однієї ситуації: цифри `3` і `8` цього шрифту після
    зведення до спільного розміру розходяться всього на 12-16, тобто на самій
    межі відриву, і будь-яке нове накреслення робить читання мовчазним. У цеху
    ця відмова повторювалась тисячами: 2520 разів на 150i-Olejka, 1102 на
    250i-Sec, 926 на 250i-Tolik (скринька екранів, 27.09.26).

    Вгадувати ми при цьому не починаємо: список віддається ВИКЛИКАЧЕВІ, а той
    лишає рядок, лише коли з ним збігається РІВНО ОДНА робота живої черги —
    той самий другий незалежний сигнал, що вже потрібен і для впевненого
    читання (`_program_from_screen`). Не зійшлось — мовчимо, як раніше:
    хибне число гірше за жодне.
    """
    templates = load_newgen_glyphs()
    if not templates:
        return []
    # Кадр ріжемо один раз на виклик: той самий розклад дає й хвіст (через
    # `_tail_picks`), і початок назви. Цей шлях іде на КОЖНОМУ тіку, доки
    # шрифт не вирішив цифру (у цеху — тисячі разів), тож друга сегментація
    # (~15 мс) старила б решту верстатів у черзі опитування.
    layout = _name_layout(image)
    _layout_memo.pair = (image, layout)
    try:
        picks, _why = _tail_picks(image, templates)
    finally:
        _layout_memo.pair = None
    if picks is None:
        return []
    unsure = [p for p in picks if p.char is None]
    if not unsure or len(unsure) > UNSURE_LIMIT:
        return []
    combos = 1
    for pick in unsure:
        combos *= max(1, len(pick.options))
    if combos > VARIANTS_LIMIT:
        return []
    combos_text = product(*[p.options if p.char is None else (p.char,) for p in picks])
    texts = [t for t in ("".join(c) for c in combos_text) if _program_from_text(t)[0] is not None]
    if not texts:
        return []
    # Початок назви від хвоста не залежить: непевна цифра хвоста не робить
    # непевним «1_14-MONOLITH».
    prefix = _name_prefix(layout)[0] if layout is not None else ""
    programs: "list[MillingProgram]" = []
    for text in texts:
        program, _bad = _program_from_text(text, prefix)
        if program is not None:
            programs.append(program)
    return programs


def read_newgen_program(image: Image.Image) -> Optional[MillingProgram]:
    """Програма з рядка ▶ на екрані JOBS. None — якщо хоч щось не впевнено.

    Наявність Sum3D ID у черзі перевіряє викликач: тут бази немає."""
    return read_newgen_program_explained(image)[0]


def read_newgen_program_explained(
    image: Image.Image,
) -> tuple[Optional[MillingProgram], Optional[str]]:
    """Те саме, що `read_newgen_program`, плюс ЧОМУ не прочитано.

    Причина буває лише тоді, коли рядок ▶ на екрані Є, а назву не взято, —
    тобто читач мав прочитати й не зміг. Інший екран (RemiCORE, SUMMARY,
    шпалери) — це не відмова, а «тут нема чого читати»: причина None.

    Навіщо (10.09.26): перший день у цеху всі три екрани JOBS давали «не
    прочитано» без жодного сліду — одна цифра в новій позиції, одна крапка.
    Мовчазна відмова виглядала як «фіча не працює», і причину можна було
    дізнатись лише з кадру, привезеного з цеху. Тепер вона пишеться в лог
    разом із номером символу — видно, що саме донавчити."""
    templates = load_newgen_glyphs()
    if not templates:
        return None, None
    # Кадр ріжемо ОДИН раз: ті самі символи дають і хвіст, і початок назви
    # (сегментація — ~15 мс з ~20 на кадр JOBS, двічі на кожен тік не варто).
    layout = _name_layout(image)
    picks, why = _tail_picks_in(layout.glyphs if layout is not None else None, templates)
    if picks is None:
        return None, why
    for index, pick in enumerate(picks):
        if pick.char is not None:
            continue
        # Правдоподібні варіанти Є, але шрифт не дав відриву. Строгий читач
        # мовчить, як і раніше; розвʼязати таке може лише черга
        # (`read_newgen_program_variants`).
        near = ", ".join(pick.options[:2])
        return None, (
            f"цифру №{index + 1} хвоста не впізнано (схожі: {near}; треба відрив "
            f"≥{MIN_MARGIN:.0f}) — донавчити: scripts/newgen_glyphs.py learn"
        )
    text = "".join(p.char or "" for p in picks)
    program, bad = _program_from_text(text)
    if program is None:
        return None, bad
    prefix = _name_prefix(layout)[0] if layout is not None else ""
    return _program_from_text(text, prefix) if prefix else (program, None)
