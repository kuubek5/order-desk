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
спільного розміру, і порівнюється відстань до еталонів: варіанти ОДНІЄЇ цифри
лежать у межах ~22, РІЗНІ цифри — не ближче ~40 (найтісніша пара 6/5). Поріг
і вимога відриву стоять посередині цього проміжку.

Той самий прийом дає ще й перенос між верстатами: у 150i шрифт більший
(висота 22 проти 18 у 250i), але після зведення до спільного розміру цифри
150i впізнаються еталонами 250i — перевірено на всіх 20 цифрах кадру 150i.
Тому еталони спільні, а цифри, яких на одному верстаті ще не траплялось,
береться з іншого.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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
BLOCK_GAP = 40
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


def _name_glyphs(image: Image.Image) -> Optional[list[_Glyph]]:
    """Символи назви поточної програми, рядок за рядком, зліва направо."""
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
    zone = np.asarray(image.crop((rx0, ry0, rx1, ry1)).convert("RGB")).astype(np.int16)
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
    for top, bottom in _runs(name.any(axis=1)):
        line = name[top:bottom]
        # Базова лінія — останній рядок пікселів, де чорнило стоїть багатьма
        # окремими відрізками (низи всіх символів). Нижче лишається лише `_`.
        pieces = [len(_runs(row)) for row in line]
        busiest = max(pieces)
        base = max(i for i, n in enumerate(pieces) if n >= max(4, 0.3 * busiest))
        cap_band = line[: base + 1]
        for a, b in _runs(cap_band.any(axis=0)):
            column = cap_band[:, a:b]
            rows = np.flatnonzero(column.any(axis=1))
            glyphs.append(_Glyph(bits=column[rows[0]: rows[-1] + 1], top=int(rows[0]), cap=base + 1))
    return glyphs


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


def _classify_digit(glyph: _Glyph, templates: dict[str, list[np.ndarray]]) -> Optional[str]:
    """Цифра або None, якщо немає впевненого переможця."""
    h, w = glyph.bits.shape
    if h < glyph.cap * 0.9 or not templates:
        return None
    ranked = _ranked(glyph, templates)
    char, distance = ranked[0]
    if distance > MAX_DISTANCE:
        return None
    if len(ranked) > 1 and ranked[1][1] - distance < MIN_MARGIN:
        return None
    return char


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
    glyphs = _name_glyphs(image)
    # Короткий рядок біля кружка — не назва програми (SUMMARY теж має кружок
    # і кілька слів поруч): читати нема чого, це не відмова.
    if glyphs is None or len(glyphs) < len(TAIL):
        return None, None
    tail = glyphs[-len(TAIL):]
    text = []
    for index, (glyph, want) in enumerate(zip(tail, TAIL)):
        if want == "?":
            text.append("?")
            continue
        if want == "#":
            got = _classify_digit(glyph, templates)
            if got is None or not got.isdigit():
                ranked = _ranked(glyph, templates)
                near = ", ".join(f"{c}={d:.0f}" for c, d in ranked[:2])
                return None, (
                    f"цифру №{index + 1} хвоста не впізнано ({near}; треба ≤{MAX_DISTANCE:.0f} "
                    f"і відрив ≥{MIN_MARGIN:.0f}) — донавчити: scripts/newgen_glyphs.py learn"
                )
        else:
            got = _shape_class(glyph)
            if got != want:
                h, w = glyph.bits.shape
                return None, (
                    f"символ №{index + 1} хвоста мав бути «{want}», а форма {w}×{h}, "
                    f"заповнення {glyph.bits.mean():.2f}"
                )
        text.append(got)
    s = "".join(text)
    # ####-##-## ##-##-## .I??
    year, month, day = s[0:4], s[5:7], s[8:10]
    hour, minute, second = s[10:12], s[13:15], s[16:18]
    if not (2020 <= int(year) <= 2099 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None, f"прочитано неможливу дату {year}-{month}-{day}"
    if not (int(hour) < 24 and int(minute) < 60 and int(second) < 60):
        return None, f"прочитано неможливий час {hour}-{minute}-{second}"
    date = f"{year}-{month}-{day}"
    time = f"{hour}-{minute}-{second}"
    return MillingProgram(iso_name=f"{date}_{time}.iso", sum3d_id=time, date=date), None
