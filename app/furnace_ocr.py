"""Читання табло печі Austromat (B&R Lasal) з кадру VNC.

Чому взагалі піксельне читання. У печі є чистий канал даних — файл
`DATALOG.dat` по FTP, — але він під паролем користувача `customer`, якого нам
поки не дали. Єдине, що піч віддає без пароля, — це екран по VNC. Тому дані ми
ЧИТАЄМО З КАРТИНКИ, і весь модуль побудований навколо одного правила:

    хибне число гірше за жодне.

На термінах спікання «лишилось 6 хв» замість «26 хв» коштує партії робіт.
Тому тут немає жодного «схоже на». Кожна цифра або збігається з еталоном
піксель-у-піксель (з малим допуском на згладжування), або лишається
невпізнаною — і тоді ВСЕ поле повертається як None. Оператор бачить сирий кроп
зони й читає очима, тобто гірший випадок дорівнює тому, що є зараз.

Чому шаблони, а не tesseract. Табло малює фіксований растровий шрифт у
фіксованих координатах — тут не потрібне розпізнавання, достатньо порівняння.
Порівняння дає або точну відповідь, або чесне «не знаю», тоді як tesseract
радо поверне «1» замість «7» без жодного натяку на сумнів. Плюс жодного
зовнішнього бінарника в інсталяторі.

Еталони цифр лежать в app/data/furnace_glyphs.json, знімаються з реального
кадру скриптом scripts/furnace_glyphs.py. Дрібний шрифт (висота 14 px —
годинники, «срок», поточна команда) укомплектований повністю. Великий
червоний шрифт температури (висота 17 px) поки має не всі цифри: два
калібрувальні кадри (холодна й гаряча піч) показали лише 0,4,5,7,9. Решта
доб'ється одним прогоном скрипта на робочому ПК.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from app.runtime import resource_path

# Рідне розділення панелі Lasal. Зони нижче — піксельні координати В ЦІЙ
# системі; кадр іншого розміру ми свідомо НЕ масштабуємо (див. read_panel).
PANEL_SIZE = (800, 600)

logger = logging.getLogger(__name__)

GLYPHS_PATH = "app/data/furnace_glyphs.json"


# ── Як відрізнити чорнило від тла в кожній зоні ─────────────────────────────
# Три різні трактування замість одного «порогу яскравості»: температура —
# червоне по білому, «срок» і команда — біле по чорному, годинники — чорне по
# сірому. Один спільний поріг не працює для всіх трьох.

def _ink_red(p: tuple[int, int, int]) -> bool:
    return p[0] - p[1] > 60 and p[0] - p[2] > 60


def _ink_light(p: tuple[int, int, int]) -> bool:
    return p[0] + p[1] + p[2] > 450


def _ink_dark(p: tuple[int, int, int]) -> bool:
    return p[0] + p[1] + p[2] < 300


INKS: dict[str, Callable[[tuple[int, int, int]], bool]] = {
    "red": _ink_red,
    "light": _ink_light,
    "dark": _ink_dark,
}


@dataclass(frozen=True)
class Zone:
    """Прямокутник табло, з якого щось читаємо.

    rect — (left, top, right, bottom) у координатах панелі 800×600, обрізаний
    ВСЕРЕДИНІ рамки поля: рамка — суцільна лінія чорнила, і сегментатор
    прийняв би її за цифру.
    """

    rect: tuple[int, int, int, int]
    ink: str
    title: str
    # Яким МУСИТЬ бути прочитаний рядок. Це другий запобіжник після
    # порівняння з еталонами: пошкоджена цифра іноді розсипається на уламки,
    # які проходять як розділовий знак («7» → «.»), і саме шаблон ловить те,
    # що окрема цифра зловити не може. Не збігся — поле порожнє.
    pattern: str = r"^.+$"


ZONES: dict[str, Zone] = {
    # «Tc 759 °C» — праву межу навмисно поставлено ДО значка градуса, інакше
    # кружечок «°» став би зайвим сегментом.
    "temp": Zone((55, 22, 132, 56), "red", "Температура", r"^\d{1,4}$"),
    # RUN / WAIT — читається не текстом, а кольором (див. read_status).
    "status": Zone((730, 44, 795, 72), "dark", "Статус"),
    # «срок 01:59:26» — залишок ПОТОЧНОЇ КОМАНДИ рецепту, не програми.
    # Розібрано на живій печі 30.08.26: рецепт із табло
    # (T008.A990 … C1150 T1800 … C1530 T7200 … A200) дає перший крок «нагрів
    # 20→990 при 8°/хв» = 2,02 год, і «срок» показував рівно 01:59:26.
    # Оператору це число не потрібне — його цікавить, коли ВІДКРИВАТИ піч.
    # Лишається як третій сигнал статусу (ненульове = щось іде).
    "step": Zone((506, 421, 627, 441), "light", "Крок", r"^\d{2}:\d{2}:\d{2}$"),
    # «Текущая команда» — рядок рецепту (T008.A990) або C0 у спокої.
    # Рядок рецепту виглядає як «T008.A990» або «C0» — тобто в ньому Є і
    # літера, і цифра. Старий шаблон приймав будь-який набір дозволених
    # символів, тож «........» чи «0000» (типовий вигляд змазаного кропу)
    # проходили як справжня команда (ревʼю 07.09.26, D.3).
    "command": Zone(
        (281, 421, 402, 441), "light", "Команда",
        r"^(?=.*[A-Z])(?=.*\d)[0-9A-Z.]{2,12}$",
    ),
    # Два лічильники вгорі. ЛІВИЙ іде вперед — це час доби. ПРАВИЙ іде НАЗАД:
    # на двох кадрах поспіль 09:24:02 → 09:22:34, тобто годинником він бути не
    # може, хоч раніше так і був підписаний. Це залишок УСІЄЇ програми: сума
    # кроків того ж рецепту дає 9 год 23 хв, а табло показувало 9:22:34.
    # Саме з нього рахується час відкриття печі.
    "clock": Zone((552, 4, 672, 28), "dark", "Час доби", r"^\d{2}:\d{2}:\d{2}$"),
    "remaining": Zone((682, 4, 793, 28), "dark", "Лишилось", r"^\d{2}:\d{2}:\d{2}$"),
    # Кнопка внизу: червона «Отменить программу» / зелена «Запустить».
    "button": Zone((545, 535, 660, 595), "dark", "Кнопка"),
}

# Смужки кадру для звірки очима — кожна лягає ПІД своїм числом на плитці.
# Навмисно вузькі й у рідному масштабі 1:1: широка смуга на всю ширину табло
# стискалась би вдвічі, і цифри на ній ставали б нечитабельними — тобто
# перевірка, заради якої вона існує, зникала б.
EYE_CROPS: dict[str, tuple[int, int, int, int]] = {
    "temp": (20, 18, 175, 58),
    # Смужка під числом «Лишилось» мусить показувати ТОЙ САМИЙ лічильник, який
    # ми читаємо — правий верхній (залишок усієї програми). Раніше тут стояв
    # «срок» унизу екрана, тобто доказ був від іншого числа.
    "srok": (676, 0, 800, 32),
    "status": (700, 40, 800, 78),
}

# Кольори, за якими визначається стан. Зміряні на реальних кадрах обох станів.
RUN_GREEN = (0, 170, 0)
WAIT_GREY = (148, 146, 148)
BUTTON_RUNNING_RED = (255, 85, 82)
BUTTON_IDLE_GREEN = (0, 243, 0)
_COLOR_TOLERANCE = 40
_MIN_COLOR_PIXELS = 40

STATUS_RUN = "RUN"
STATUS_WAIT = "WAIT"
STATUS_UNKNOWN = "?"

# ЗБІГ МУСИТЬ БУТИ ТОЧНИЙ. Раніше тут стояв допуск 15% «на антиаліасинг», і
# коментар запевняв, що відстань між різними цифрами понад 25%. На живій печі
# це виявилось неправдою: невідома «8» відрізнялась від еталона «3» на 23
# пікселі з 187, тобто 12% — і пройшла як упевнений збіг. Табло показувало
# 138, застосунок показав 133.
#
def _cache_only_success(load):
    """Кеш, який запамʼятовує лише НЕПОРОЖНІЙ результат.

    Замість `lru_cache`. Різниця принципова: завантажувачі еталонів свідомо
    гасять помилку читання й повертають `{}` — а `lru_cache` закріпив би цю
    порожнечу до кінця життя процесу. Один транзієнтний збій диска чи мережевої
    теки тихо й НАЗАВЖДИ вимикав би читання чисел, і причину шукали б як
    «раптом перестало» (ревʼю 04.09.26 — верстати; аудит 08.09.26 — печі, де
    того ж виправлення бракувало).

    Живе тут, а не в `app/machine_ocr.py`, бо саме сюди йде наявна залежність:
    верстати вже імпортують примітиви розпізнавання з печей, і зворотний
    імпорт дав би цикл.
    """
    box: dict[str, object] = {}

    @wraps(load)
    def wrapper():
        if "value" in box:
            return box["value"]
        value = load()
        if value:
            box["value"] = value
        return value

    def cache_clear() -> None:
        box.pop("value", None)

    wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
    return wrapper


# Вигадане число тут гірше за порожнє поле: за температурою судять, чи піч
# вийшла на режим. Тому — рівно нуль розбіжностей.
#
# Допуск і не потрібен: панель приходить сирим фреймбуфером VNC, без
# масштабування й без згладжування. Виміряно на дев'яти кадрах живої печі —
# 87 символів з 89 збіглися піксель-у-піксель. Два, що не збіглися, і були
# двома справжніми знахідками: невідома «8» та інший варіант накреслення «4».
# Незнайоме накреслення тепер видно як «?» і доучується явно, а не вгадується.
_MAX_MISMATCH_PIXELS = 0


# Той самий кеш, що на верстатах (`app/machine_ocr._cache_only_success`), а не
# `lru_cache`. Різниця принципова: завантажувач нижче свідомо гасить відсутність
# файлу й повертає `{}` — а `lru_cache` закріпив би цю порожнечу до кінця життя
# процесу. Один транзієнтний збій диска тихо й НАЗАВЖДИ вимикав би всі числа на
# печах, і причину шукали б як «раптом перестало». На верстатах це вже
# виправлено 04.09.26, на печі виправлення не перенесли (аудит 08.09.26).
@_cache_only_success
def load_glyphs() -> dict[int, dict[str, list[list[str]]]]:
    """Еталони цифр: висота шрифту → цифра → список бітмап-варіантів."""
    path = Path(resource_path(GLYPHS_PATH))
    if not path.exists():
        # Мовчазне «нічого не знаю» — найгірша з можливих відповідей: статус
        # читається кольором і далі працює, а ВСІ числа зникають, і причина
        # ніде не написана. Саме так це виглядало у зібраному застосунку.
        logger.error(
            "Еталони цифр не знайдено: %s — числа з табло читатись не будуть", path
        )
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {int(h): chars for h, chars in raw.get("fonts", {}).items()}


@dataclass
class Field:
    """Результат читання однієї зони.

    text — розпізнаний рядок («00:26:59») або None, якщо хоч один символ не
    впізнано. `unknown` каже, скільки символів провалилось: це те, що показуємо
    в діагностиці, коли просимо доповнити еталони.
    """

    text: Optional[str] = None
    unknown: int = 0
    segments: int = 0
    raw: str = ""
    # Крайній символ торкався межі зони, тобто був обрізаний рамкою кропа.
    # Окреме поле, а не просто «text=None»: причини порожнього поля різні
    # (незнайоме накреслення / не той шаблон / обрізано), і в діагностиці їх
    # треба розрізняти. Заразом це робить перевірку спостережуваною в тестах,
    # не вимагаючи малювати справжні еталонні цифри.
    clipped: bool = False


@dataclass
class PanelReading:
    """Все, що вдалось прочитати з одного кадру."""

    status: str = STATUS_UNKNOWN
    temp_c: Optional[int] = None
    remaining_seconds: Optional[int] = None
    command: Optional[str] = None
    step_seconds: Optional[int] = None
    # Три незалежні сигнали «йде / не йде». Тримаємо їх окремо навмисно: коли
    # вони розійдуться, статус має стати «?», а не тихо обрати один з них.
    signals: dict[str, Optional[str]] = field(default_factory=dict)
    fields: dict[str, Field] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != STATUS_UNKNOWN and not self.warnings


def _segments(image: Image.Image, ink: Callable) -> list[tuple[int, int, int, int]]:
    """Розрізати зону на символи по порожніх колонках.

    Табло малює моноширинно й з проміжками, тож розділення колонками надійне і
    не потребує жодної евристики про очікувану кількість символів.
    """
    px = image.load()
    width, height = image.size
    filled = [any(ink(px[x, y][:3]) for y in range(height)) for x in range(width)]
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None
    for x, on in enumerate(filled):
        if on and start is None:
            start = x
        elif not on and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, width))
    ink_per_column = [
        sum(1 for y in range(height) if ink(px[x, y][:3])) for x in range(width)
    ]
    runs = _split_glued(runs, ink_per_column)
    boxes = []
    for left, right in runs:
        ys = [y for y in range(height) for x in range(left, right) if ink(px[x, y][:3])]
        boxes.append((left, min(ys), right, max(ys) + 1))
    return boxes


def _split_glued(
    runs: list[tuple[int, int]], columns: Optional[list[int]] = None
) -> list[tuple[int, int]]:
    """Розрізати склеєні сусідні символи.

    Табло монохромне й моноширинне, але деякі пари торкаються: на живій печі
    «09:23:44» приходило одним блоком 22px замість двох по 9-11 — у цього
    шрифту діагональ четвірки дотягується до сусіда. Наслідок був тихий: число
    не читалось цілком, і оператор не бачив, коли відкривати піч.

    Ріжемо ЛИШЕ те, що явно склеєне (ширина кратна типовій), і ріжемо ПО
    ДОЛИНАХ — колонках із найменшою кількістю чорнила, — а не рівними
    частинами. Рівний поділ давав частини на піксель ширші за еталон, а
    звірка тепер точна: символ інакшого розміру не збігається ні з чим, і
    «виправлення» перетворювало одну ваду на іншу.
    """
    widths = sorted(right - left for left, right in runs if right - left > 4)
    if not widths:
        return runs
    typical = widths[len(widths) // 2]
    if typical <= 0:
        return runs
    out: list[tuple[int, int]] = []
    for left, right in runs:
        span = right - left
        parts = round(span / typical)
        # Допуск пропорційний: склеєна пара «44» дала 22px при типовій 9.
        if not (2 <= parts <= 4 and abs(span - parts * typical) <= 0.4 * typical * parts):
            out.append((left, right))
            continue
        cuts = _valley_cuts(columns, left, right, parts) if columns else None
        if cuts is None:
            step = span / parts
            cuts = [left + round(i * step) for i in range(1, parts)]
        edges = [left, *cuts, right]
        out.extend((edges[i], edges[i + 1]) for i in range(len(edges) - 1))
    return out


def _valley_cuts(
    columns: list[int], left: int, right: int, parts: int
) -> Optional[list[int]]:
    """Місця розрізу — найтонші колонки біля очікуваних меж символів.

    `columns[x]` — скільки пікселів чорнила в колонці x. Шукаємо мінімум у
    вікні ±2px навколо рівномірної межі: справжня межа склеєних символів десь
    поруч, і саме там перемичка найтонша.
    """
    span = right - left
    step = span / parts
    cuts: list[int] = []
    for i in range(1, parts):
        centre = left + round(i * step)
        window = [x for x in range(centre - 2, centre + 3) if left < x < right]
        if not window:
            return None
        cuts.append(min(window, key=lambda x: (columns[x], abs(x - centre))))
    if len(set(cuts)) != len(cuts):
        return None
    return cuts


def _bitmap(image: Image.Image, box: tuple[int, int, int, int], ink: Callable) -> list[str]:
    px = image.load()
    left, top, right, bottom = box
    return [
        "".join("1" if ink(px[x, y][:3]) else "0" for x in range(left, right))
        for y in range(top, bottom)
    ]


def _match_digit(bitmap: list[str], glyphs: dict) -> Optional[str]:
    """Цифра або None. None — нормальний результат, а не збій."""
    height = len(bitmap)
    width = len(bitmap[0]) if height else 0
    table = glyphs.get(height)
    if not table or not width:
        return None
    scored: list[tuple[int, str]] = []
    for digit, variants in table.items():
        for variant in variants:
            if len(variant) != height or len(variant[0]) != width:
                continue
            distance = sum(
                1
                for row_a, row_b in zip(bitmap, variant)
                for a, b in zip(row_a, row_b)
                if a != b
            )
            scored.append((distance, digit))
    if not scored:
        return None
    scored.sort()
    best_distance, best_digit = scored[0]
    if best_distance > _MAX_MISMATCH_PIXELS:
        return None
    # Дві РІЗНІ цифри не можуть збігтися точно з одним і тим самим малюнком —
    # якщо це сталось, у сховищі дубль, і мовчки вибирати одну з них не можна.
    exact_digits = {digit for distance, digit in scored if distance <= _MAX_MISMATCH_PIXELS}
    if len(exact_digits) > 1:
        return None
    return best_digit


def accept_reading(raw: str, pattern: str, *, unknown: int, clipped: bool) -> bool:
    """Чи можна показувати прочитане число операторові.

    Три незалежні причини відмовити, і будь-якої досить:

    * `unknown` — хоч один символ не збігся з еталоном піксель-у-піксель;
    * `clipped` — крайній символ торкався межі зони, тобто був обрізаний
      рамкою кропа, і число неповне;
    * `pattern` — рядок не тієї форми, якої мусить бути (пошкоджена цифра
      іноді розсипається на уламки, що проходять як розділовий знак).

    Винесено з `read_zone` окремою функцією навмисно: саме тут живе правило
    §14 «краще порожньо, ніж правдоподібно й хибно», і воно має бути покрите
    тестом прямо, а не через малювання синтетичних цифр, які все одно не
    збігаються з еталонами (аудит 08.09.26).
    """
    if unknown or clipped:
        return False
    return bool(re.match(pattern, raw))


def read_zone(panel: Image.Image, name: str) -> Field:
    """Прочитати одну зону як рядок цифр і роздільників."""
    zone = ZONES[name]
    ink = INKS[zone.ink]
    crop = panel.crop(zone.rect).convert("RGB")
    glyphs = load_glyphs()
    out: list[str] = []
    unknown = 0
    boxes = _segments(crop, ink)
    for box in boxes:
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        # Розділові знаки. Еталонами не тримаємо — їхня форма нам не потрібна,
        # потрібне лише їхнє місце в рядку. Крапка (2×2 у рецепті T008.A990) і
        # двокрапка (2×10 у часі) розрізняються висотою.
        if width <= 3 and height <= 4:
            out.append(".")
            continue
        if width <= 3 and height <= 11:
            out.append(":")
            continue
        digit = _match_digit(_bitmap(crop, box, ink), glyphs)
        if digit is None:
            unknown += 1
            out.append("?")
        else:
            out.append(digit)
    raw = "".join(out)

    # Символ, що торкається краю зони, — обрізаний рамкою кропа, і прочитане
    # число тоді неповне. Піксельна звірка з еталонами тут безсила за
    # означенням: вона перевіряє ті цифри, що ПОТРАПИЛИ в кадр, і нічого не
    # знає про ту, що лишилась за межею.
    #
    # Для температури це найнебезпечніший випадок з усіх: шаблон `^\d{1,4}$`
    # приймає будь-яку довжину, а перевірка «не більше 2000» пропускає
    # результат. Тобто «1350» з обрізаною лівою цифрою ставало «350» —
    # правдоподібним, але хибним числом на табло. Це прямо порушує правило
    # §14: цифра або збігається з еталоном, або поле ПОРОЖНЄ (аудит 08.09.26).
    #
    # Годинники так не ламаються — їхній шаблон фіксованої довжини ловить
    # нестачу сам, — але правило однакове для всіх зон: краще порожньо.
    clipped = bool(boxes) and (boxes[0][0] <= 0 or boxes[-1][2] >= crop.size[0])
    if clipped:
        logger.warning(
            "Зона «%s»: символ торкається краю (%s) — читання відкинуто як неповне",
            zone.title, raw or "?",
        )

    text = raw if accept_reading(raw, zone.pattern, unknown=unknown, clipped=clipped) else None
    return Field(
        text=text, unknown=unknown, segments=len(boxes), raw=raw, clipped=clipped
    )


def _dominant(
    panel: Image.Image, rect: tuple[int, int, int, int], color: tuple[int, int, int]
) -> int:
    """Скільки пікселів зони близькі до заданого кольору."""
    crop = panel.crop(rect).convert("RGB")
    hits = 0
    # Межа рівно за розміром зони. З великою межею (1<<20) Pillow щоразу
    # виділяє таблицю на мільйон кольорів — 6.5 мс на виклик замість нуля, а
    # викликів чотири на кадр. Різних кольорів у зоні фізично не більше, ніж
    # пікселів, тож тісна межа нічого не втрачає.
    for count, pixel in crop.getcolors(maxcolors=crop.width * crop.height) or []:
        if all(abs(a - b) <= _COLOR_TOLERANCE for a, b in zip(pixel, color)):
            hits += count
    return hits


def read_status(panel: Image.Image) -> tuple[str, dict[str, Optional[str]]]:
    """Стан печі за незалежними сигналами.

    1. Слово вгорі праворуч: зелене RUN / сіре WAIT.
    2. Кнопка внизу: червона «Отменить» (є що скасовувати) / зелена
       «Запустить» (нічого не йде).

    Голосування ДВОХ сигналів — і потрібні саме два згодні. Кожен окремо
    може збрехати (перемальовка кадру, кроп не туди), і сенс голосування в
    тому, що одному сигналу ми не віримо: раніше, коли другий не читався,
    статус усе одно брався з першого, тобто голосування вироджувалось у той
    самий «один сигнал», від якого й захищає (ревʼю 07.09.26, D.3).

    Третій сигнал («срок», залишок часу) сюди не входить: він додається вище
    за рівнем, у `read_panel`, і вміє лише СУПЕРЕЧИТИ — призначити статус сам
    не може, бо в RUN бувають нулі на межі сегментів. Тому в CLAUDE.md це
    «голосування двох плюс звірка третім», а не «голосування трьох».

    Немає двох згодних — `STATUS_UNKNOWN`: порожнє місце на екрані чесніше за
    цифру з повітря, і це те саме правило, що для температури.
    """
    word_rect = ZONES["status"].rect
    button_rect = ZONES["button"].rect
    signals: dict[str, Optional[str]] = {}

    green = _dominant(panel, word_rect, RUN_GREEN)
    grey = _dominant(panel, word_rect, WAIT_GREY)
    if green >= _MIN_COLOR_PIXELS and green > grey:
        signals["word"] = STATUS_RUN
    elif grey >= _MIN_COLOR_PIXELS and grey > green:
        signals["word"] = STATUS_WAIT
    else:
        signals["word"] = None

    red_button = _dominant(panel, button_rect, BUTTON_RUNNING_RED)
    green_button = _dominant(panel, button_rect, BUTTON_IDLE_GREEN)
    if red_button >= _MIN_COLOR_PIXELS and red_button > green_button:
        signals["button"] = STATUS_RUN
    elif green_button >= _MIN_COLOR_PIXELS and green_button > red_button:
        signals["button"] = STATUS_WAIT
    else:
        signals["button"] = None

    votes = [value for value in signals.values() if value]
    if len(votes) < 2:
        return STATUS_UNKNOWN, signals   # один сигнал — це не голосування
    if len(set(votes)) > 1:
        return STATUS_UNKNOWN, signals   # сигнали не сходяться
    return votes[0], signals


# Рівно дві цифри в кожній позиції: табло малює саме так, і «0:26:59» —
# це не інший формат, а ознака, що одну цифру ми загубили.
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")


def parse_clock(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    match = _TIME_RE.match(text)
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def parse_temperature(text: Optional[str]) -> Optional[int]:
    """Температура в °C. Вище 2000 не буває навіть у цій печі — таке значення
    означає, що ми зчитали не те, і краще віддати None."""
    if not text or not text.isdigit():
        return None
    value = int(text)
    return value if 0 <= value <= 2000 else None


def read_panel(image: Image.Image) -> PanelReading:
    """Один кадр → показання. Ніколи не кидає на «поганому» кадрі: усе, чого
    не вдалось прочитати, лишається None із поясненням у warnings."""
    reading = PanelReading()
    panel = image.convert("RGB")
    if panel.size != PANEL_SIZE:
        # Свідомо НЕ масштабуємо: зміна розміру розмиває растровий шрифт, і
        # порівняння з еталонами перетворилось би на вгадування. Кадр усе одно
        # зберігається — оператор дивиться очима, а ми дізнаємось, що на цій
        # печі інша роздільність, і калібруємо зони окремо.
        reading.warnings.append(
            f"Неочікуваний розмір екрана {panel.size[0]}×{panel.size[1]}"
            f" (очікували {PANEL_SIZE[0]}×{PANEL_SIZE[1]})"
        )
        return reading

    reading.status, reading.signals = read_status(panel)

    for name in ("temp", "remaining", "command", "step"):
        reading.fields[name] = read_zone(panel, name)

    reading.temp_c = parse_temperature(reading.fields["temp"].text)
    # remaining — залишок УСІЄЇ програми (правий лічильник), тобто те, з чого
    # рахується час відкриття. step — залишок поточної команди, лише для
    # перевірки статусу.
    reading.remaining_seconds = parse_clock(reading.fields["remaining"].text)
    reading.step_seconds = parse_clock(reading.fields["step"].text)
    reading.command = reading.fields["command"].text

    # Третій сигнал стану — залишок часу. Додається лише як перевірка: сам
    # призначити статус він не може (у RUN бувають нулі на межі сегментів),
    # але мовчазна розбіжність із двома іншими має бути видною.
    if reading.step_seconds is not None:
        expected = STATUS_RUN if reading.step_seconds > 0 else STATUS_WAIT
        reading.signals["step"] = expected
        if reading.status != STATUS_UNKNOWN and expected != reading.status:
            reading.warnings.append(
                "«срок» %s не сходиться зі статусом %s"
                % (reading.fields["step"].text, reading.status)
            )

    if not load_glyphs():
        # Причина мусить дійти до екрана, а не лишитись у лозі: без еталонів
        # порожні поля виглядають як «піч мовчить», хоча піч якраз відповідає.
        reading.warnings.append(
            "Немає файлу еталонів цифр — жодне число з табло не читається"
        )
    if reading.temp_c is None:
        reading.warnings.append("Температуру не розпізнано")
    if reading.remaining_seconds is None:
        reading.warnings.append("Залишок часу не розпізнано")

    return reading


def format_remaining(seconds: Optional[int]) -> str:
    """Людський вигляд залишку. «—» для None: порожнє місце краще за нуль,
    який виглядає як справжнє «нуль хвилин»."""
    if seconds is None:
        return "—"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
