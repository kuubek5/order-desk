"""Читання екрана SLM-принтера SISMA з кадру агента.

Чому окремий модуль, а не гілка в `machine_ocr`. Там усе побудоване навколо
СМУГИ прогресу: знайти прямокутник, зміряти частку заливки, звірити з
підписом. SISMA нічого міряти не змушує — вона пише точні числа текстом
(`Current slice: 250/1049`) і сама рахує, коли закінчить
(`Previewed End of work: 2026-09-06 20:58`). Тому тут не геометрія, а
читання рядків шаблонами символів.

Що читаємо (рівно те, що просив власник 06.09.26):
  * працює чи ні — слово в полі `Laser Status`: `Emitting` = лазер пише шар,
    `Enabled` = лазер готовий, але не пише;
  * шар N з M — рядок `Current slice`;
  * коли закінчить — `Previewed End of work` (машина рахує це сама, нам не
    треба множити відсоток на час).

Правило те саме, що на печах і фрезерних: **або точно, або нічого**. Символ
збігається з еталоном піксель-у-піксель, інакше рядок не прочитано і поле
лишається порожнім. Хибний час завершення гірший за відсутній: за ним
підуть планувати зміну.

Чому шаблони, а не бібліотечний OCR: екран малює той самий Windows тим
самим шрифтом у тій самій роздільності, тож символи виходять біт-у-біт
однакові (перевірено на 130 бойових кадрах — 23 з них із роботою). Там, де
зображення детерміноване, розпізнавання «на око» лише додало б помилок.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from app.machine_ocr import _cache_only_success
from app.runtime import resource_path

logger = logging.getLogger(__name__)

SISMA_GLYPHS_PATH = "app/data/sisma_glyphs.json"

# Два набори еталонів із РІЗНИМ порогом чорнила.
#
# «dark» — звичайний чорний текст на світло-сірому тлі (рядки шарів і часу).
# «status» — кольорове слово стану лазера: зелений `Enabled` темний, але
# блакитний `Emitting` світліший за 128 і при спільному порозі просто зникав
# би з маски. Набори не змішуються: один і той самий символ у них має різні
# краї згладжування, і спільна таблиця давала б хибні збіги.
# Поріг 96, а не «десь посередині»: при 128 згладжування зшивало сусідні
# літери в одну пляму («ta», «rte»), і рядок ділився на менше символів, ніж
# у ньому є. Заміряно на бойовому кадрі — при 96 і нижче поділ стабільний.
INK_DARK = 96
INK_STATUS = 200

# Набори еталонів. `dark` — звичайний текст рядків; `bold` — ЖИРНІ підписи
# полів («Laser Status»): у них та сама висота, але інші пікселі, і в одній
# таблиці вони конфліктували б за той самий символ (спіймано при навчанні
# 06.09.26). `status` — кольорове слово стану лазера.
SET_DARK = "dark"
SET_BOLD = "bold"
# Слово стану лазера впізнається ЦІЛИМ рядком, а не символами. Причина
# бойова: `Emitting` блакитне, `Enabled` зелене, і в тих самих літер («E»,
# «n») виходять різні краї згладжування — посимвольні еталони конфліктували
# між двома словами. Слів усього два й вони відомі наперед, тож порівняння
# цілого сліду і простіше, і суворіше.
SET_WORDS = "words"

# Зони пошуку — частками розміру кадру, а не пікселями: той самий екран на
# іншій роздільності лишиться на своєму місці відносно країв. Зони навмисно
# з запасом, бо всередині ми не «беремо, що лежить», а ВИМАГАЄМО впізнати
# підпис рядка; зайве місце нічого не псує.
ZONE_SLICE = (0.68, 0.83, 1.00, 0.89)      # «Current slice: 250/1049»
# Права межа 0.635 — між кінцем тексту і піктограмою лампочки. Вужче
# обрізало останню цифру дати (кількість символів при цьому НЕ мінялась, лише
# ширина сліду — і еталон «6» тихо ставав іншим); ширше в рядок часу залазив
# край іконки.
ZONE_TIMES = (0.355, 0.905, 0.635, 0.995)  # «Work started…» / «Previewed End…»
ZONE_LASER = (0.00, 0.40, 0.17, 0.52)      # «Laser Status» + слово стану

LABEL_SLICE = "Currentslice:"
LABEL_STARTED = "Workstarted:"
LABEL_ENDS = "PreviewedEndofwork:"
WORD_EMITTING = "Emitting"
WORD_ENABLED = "Enabled"

_SLICE_RE = re.compile(r"^Currentslice:(\d+)/(\d+)$")
_STAMP_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class SismaReading:
    """Прочитане з одного кадру. Кожне поле незалежне: рядок, який не
    прочитався, лишає своє поле порожнім і не чіпає решту."""

    # Друк іде — тобто програма активна. Ознака: на екрані взагалі є рядок
    # `Current slice`; на простої його немає.
    printing: Optional[bool] = None     # None = стан не прочитано
    # Саме зараз лазер пише шар. МІЖ шарами машина розрівнює порошок, і в цей
    # час вона показує `Enabled`, хоча робота йде — на бойових кадрах так
    # вийшло тричі з 23. Тому «працює» рахуємо за рядком шару, а слово лазера
    # каже лише ФАЗУ. Якби ми злили ці два поняття, оператор бачив би «стоїть»
    # щоразу, коли машина розрівнює порошок.
    lasing: Optional[bool] = None
    laser_word: str = ""                # що саме написано (для журналу)
    layer: Optional[int] = None
    layers_total: Optional[int] = None
    started_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None

    @property
    def percent(self) -> Optional[int]:
        """Відсоток із ЧИСЕЛ, а не з геометрії смуги — тому точний."""
        if not self.layer or not self.layers_total:
            return None
        if self.layer > self.layers_total:
            # Розсинхрон читання (шар не може бути більший за всього) —
            # мовчимо, замість показати 130%.
            return None
        return round(self.layer * 100 / self.layers_total)


# НЕ lru_cache: він закріпив би порожній `{}` після одного транзієнтного
# збою читання до кінця життя процесу, і SISMA «раптом» перестала б
# читатись до рестарту (та сама пастка, що й у machine_ocr, ревʼю 07.09.26).
@_cache_only_success
def load_sisma_glyphs() -> dict[str, dict[int, dict[str, tuple[tuple[int, ...], ...]]]]:
    """Еталони символів: набір → висота → символ → бітова матриця.

    Висота в ключі, бо в одному наборі співіснують заголовки й основний
    текст: символи різної висоти ніколи не мусять порівнюватись між собою.
    """
    path = Path(resource_path(SISMA_GLYPHS_PATH))
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Еталони SISMA не прочитались")
        return {}
    out: dict[str, dict[int, dict[str, tuple[tuple[int, ...], ...]]]] = {}
    for set_name, by_height in raw.items():
        out[set_name] = {}
        for height, chars in by_height.items():
            out[set_name][int(height)] = {
                char: tuple(tuple(1 if c == "#" else 0 for c in row) for row in rows)
                for char, rows in chars.items()
            }
    return out


def _mask(image: Image.Image, ink: int) -> list[list[int]]:
    grey = image.convert("L")
    px = grey.load()
    w, h = grey.size
    return [[1 if px[x, y] < ink else 0 for x in range(w)] for y in range(h)]


def _text_lines(mask: list[list[int]]) -> list[tuple[int, int]]:
    """Межі рядків тексту — смуги рядків, де є хоч один піксель чорнила."""
    h, w = len(mask), len(mask[0]) if mask else 0
    inked = [any(mask[y][x] for x in range(w)) for y in range(h)]
    lines, y = [], 0
    while y < h:
        if not inked[y]:
            y += 1
            continue
        y0 = y
        while y < h and inked[y]:
            y += 1
        lines.append((y0, y))
    return lines


def _glyphs(mask: list[list[int]], y0: int, y1: int) -> list[tuple[int, tuple[tuple[int, ...], ...]]]:
    """Символи рядка: суцільні по горизонталі групи колонок із чорнилом.

    Пробіли НЕ відновлюємо: у підписах вони й не потрібні (порівнюємо з
    текстом без пробілів), а ділити за шириною проміжку — вгадування.
    """
    w = len(mask[0])
    cols = [any(mask[y][x] for y in range(y0, y1)) for x in range(w)]
    out, x = [], 0
    while x < w:
        if not cols[x]:
            x += 1
            continue
        xs = x
        while x < w and cols[x]:
            x += 1
        rows = [y for y in range(y0, y1) if any(mask[y][xx] for xx in range(xs, x))]
        if not rows:
            continue
        bits = tuple(
            tuple(mask[y][xx] for xx in range(xs, x)) for y in range(rows[0], rows[-1] + 1)
        )
        out.append((xs, bits))
    return out


def _decode_line(
    mask: list[list[int]], y0: int, y1: int, templates: dict[int, dict[str, tuple]]
) -> Optional[str]:
    """Рядок у текст або None, якщо хоч один символ незнайомий.

    Саме «або весь рядок, або нічого»: половина прочитаного рядка — це
    вигадане число, а не часткова відповідь.
    """
    text = []
    for _, bits in _glyphs(mask, y0, y1):
        height = len(bits)
        char = None
        for candidate, ref in templates.get(height, {}).items():
            if ref == bits:
                char = candidate
                break
        if char is None:
            return None
        text.append(char)
    return "".join(text) if text else None


def _zone(image: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    w, h = image.size
    return image.crop((int(w * box[0]), int(h * box[1]), int(w * box[2]), int(h * box[3])))


def _lines_in(image: Image.Image, box, ink: int, templates) -> list[str]:
    zone = _zone(image, box)
    if zone.width < 4 or zone.height < 4:
        return []
    mask = _mask(zone, ink)
    out = []
    for y0, y1 in _text_lines(mask):
        decoded = _decode_line(mask, y0, y1, templates)
        if decoded:
            out.append(decoded)
    return out


def _parse_stamp(text: str) -> Optional[datetime]:
    match = _STAMP_RE.search(text)
    if match is None:
        return None
    year, month, day, hour, minute = (int(v) for v in match.groups())
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _line_signature(mask: list[list[int]], y0: int, y1: int) -> Optional[tuple[tuple[int, ...], ...]]:
    """Слід рядка цілком, обрізаний по чорнилу.

    Саме цілком: у слові стану літери двох слів різного кольору дають різні
    краї, тож посимвольне порівняння тут не працює (див. SET_WORDS).
    """
    glyphs = _glyphs(mask, y0, y1)
    if not glyphs:
        return None
    w = len(mask[0])
    xs = [x for x in range(w) if any(mask[y][x] for y in range(y0, y1))]
    ys = [y for y in range(y0, y1) if any(mask[y][x] for x in range(w))]
    return tuple(tuple(mask[y][x] for x in range(xs[0], xs[-1] + 1)) for y in ys)


def read_laser_word(image: Image.Image) -> str:
    """`Emitting` / `Enabled` — або порожньо, якщо слід не збігся з еталоном."""
    words = load_sisma_glyphs().get(SET_WORDS, {})
    if not words:
        return ""
    zone = _zone(image, ZONE_LASER)
    mask = _mask(zone, INK_STATUS)
    for y0, y1 in _text_lines(mask):
        signature = _line_signature(mask, y0, y1)
        if signature is None:
            continue
        for word, ref in words.get(len(signature), {}).items():
            if ref == signature:
                return word
    return ""


def read_sisma(image: Image.Image) -> SismaReading:
    """Прочитати кадр SISMA. Ніколи не кидає: збій читання = порожні поля."""
    try:
        return _read_sisma(image)
    except Exception:  # noqa: BLE001 — читання кадру не має валити опитування
        logger.exception("Кадр SISMA не прочитано")
        return SismaReading()


def _read_sisma(image: Image.Image) -> SismaReading:
    glyphs = load_sisma_glyphs()
    dark = glyphs.get(SET_DARK, {})
    if not dark:
        return SismaReading()

    layer = total = None
    for line in _lines_in(image, ZONE_SLICE, INK_DARK, dark):
        match = _SLICE_RE.match(line)
        if match:
            layer, total = int(match.group(1)), int(match.group(2))
            break

    started_at = ends_at = None
    # Рядок «Previewed End of work» на екрані переноситься: дата лишається в
    # першому рядку, час падає в наступний. Тому склеюємо підписаний рядок із
    # тим, що йде за ним, і аж потім шукаємо штамп.
    time_lines = _lines_in(image, ZONE_TIMES, INK_DARK, dark)
    for index, line in enumerate(time_lines):
        joined = line + (time_lines[index + 1] if index + 1 < len(time_lines) else "")
        if line.startswith(LABEL_STARTED):
            started_at = _parse_stamp(line) or _parse_stamp(joined)
        elif line.startswith(LABEL_ENDS):
            ends_at = _parse_stamp(line) or _parse_stamp(joined)

    laser_word = read_laser_word(image)
    lasing: Optional[bool] = None
    if laser_word == WORD_EMITTING:
        lasing = True
    elif laser_word == WORD_ENABLED:
        lasing = False

    # «Друкує» = є рядок шару. Слово лазера це НЕ спростовує: `Enabled` при
    # живому рядку означає паузу між шарами, а не простій.
    #
    # А от `Emitting` БЕЗ рядка шару лишається суперечністю — лазер не може
    # писати шар, якого на екрані немає. Такий кадр ми не розуміємо, і чесніше
    # не називати стан узагалі.
    printing: Optional[bool] = layer is not None
    if lasing is True and layer is None:
        printing = None
    if laser_word == "" and layer is None:
        # Ані слова, ані рядка — можливо, взагалі не той екран.
        printing = None

    return SismaReading(
        printing=printing,
        lasing=lasing,
        laser_word=laser_word,
        layer=layer,
        layers_total=total,
        started_at=started_at,
        ends_at=ends_at,
    )


def screen_is_sisma(image: Image.Image) -> bool:
    """Чи це взагалі екран SISMA — щоб не читати ним RemiCORE.

    Ознака — підпис `Laser Status` у лівій колонці: він є і в роботі, і в
    простої, і його немає на жодному іншому екрані цеху.

    Читається набором `dark`, а не `status`: заголовок жирний і чорний, і при
    порозі 200 його букви злипаються в кашу через згладжування.
    """
    glyphs = load_sisma_glyphs().get(SET_BOLD, {})
    if not glyphs:
        return False
    return any(
        line.startswith("LaserStatus")
        for line in _lines_in(image, ZONE_LASER, INK_DARK, glyphs)
    )
