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

* синім вважається піксель, де синій канал помітно переважає червоний і
  зелений (RemiCORE малює насичену «королівську» синь);
* смуга — найдовший горизонтальний пробіг такої сині в НИЖНІЙ частині кадру
  (панель статусу RemiCORE), заввишки хоч кілька рядків (щоб не зловити
  однопіксельну лінію чи текст);
* межі контейнера шукаються вліво/вправо від заливки по СВІТЛОМУ фону
  порожньої частини, доки не впремось у темну рамку;
* відсоток = ширина заливки / ширина контейнера.

Якщо смугу не знайдено або пропорції неправдоподібні — повертаємо None, і UI
просто не показує число.
"""

from __future__ import annotations

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


def _is_blue(px: tuple[int, int, int]) -> bool:
    r, g, b = px[0], px[1], px[2]
    return b > 90 and b - r > 40 and b - g > 25


def _is_light(px: tuple[int, int, int]) -> bool:
    """Порожня частина смуги — світла (біла/сіра), але НЕ синя."""
    r, g, b = px[0], px[1], px[2]
    return r > 150 and g > 150 and b > 150


@dataclass(frozen=True)
class ProgressBar:
    percent: int
    fill_width: int
    container_width: int
    box: tuple[int, int, int, int]  # left, top, right, bottom заливки — для доказу


def _runs_of_blue(row: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    """Горизонтальні пробіги синього в рядку → список (start, end_exclusive)."""
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None
    for x, px in enumerate(row):
        if _is_blue(px):
            if start is None:
                start = x
        elif start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(row)))
    return runs


def find_progress_bar(image: Image.Image) -> Optional[ProgressBar]:
    """Знайти смугу прогресу й порахувати відсоток. None — якщо не впевнені."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    if width < 80 or height < 60:
        return None

    top = int(height * BOTTOM_BAND)
    px = rgb.load()

    # 1) Найдовший пробіг синього в нижній смузі кадру.
    best: Optional[tuple[int, int, int]] = None  # (довжина, y, x0), x1 окремо
    best_span: tuple[int, int] = (0, 0)
    for y in range(top, height):
        row = [px[x, y] for x in range(width)]
        for x0, x1 in _runs_of_blue(row):
            if x1 - x0 < MIN_FILL_WIDTH:
                continue
            if best is None or (x1 - x0) > best[0]:
                best = (x1 - x0, y, x0)
                best_span = (x0, x1)
    if best is None:
        return None

    _, y_seed, _ = best
    x0, x1 = best_span

    # 2) Висота смуги: скільки сусідніх рядків мають ту саму заливку.
    def row_matches(y: int) -> bool:
        if y < 0 or y >= height:
            return False
        return _is_blue(px[x0, y]) and _is_blue(px[max(x0, x1 - 1), y])

    y_top = y_seed
    while row_matches(y_top - 1):
        y_top -= 1
    y_bot = y_seed
    while row_matches(y_bot + 1):
        y_bot += 1
    bar_height = y_bot - y_top + 1
    if bar_height < MIN_BAR_HEIGHT or bar_height > MAX_BAR_HEIGHT:
        return None

    # 3) Контейнер: від заливки вправо по СВІТЛОМУ (порожня частина), вліво —
    #    доки заливка/світле. Міряємо по середньому рядку смуги.
    y_mid = (y_top + y_bot) // 2
    left = x0
    while left - 1 >= 0 and (_is_blue(px[left - 1, y_mid]) or _is_light(px[left - 1, y_mid])):
        left -= 1
    right = x1
    while right < width and (_is_blue(px[right, y_mid]) or _is_light(px[right, y_mid])):
        right += 1

    container = right - left
    fill = x1 - left
    if container < MIN_CONTAINER_WIDTH or fill <= 0 or fill > container:
        return None

    percent = round(fill * 100 / container)
    if percent < 0 or percent > 100:
        return None
    return ProgressBar(
        percent=percent, fill_width=fill, container_width=container,
        box=(left, y_top, right, y_bot + 1),
    )


def read_progress_percent(image: Image.Image) -> Optional[int]:
    """Відсоток виконання програми або None. Тонка обгортка для сервісу."""
    bar = find_progress_bar(image)
    return bar.percent if bar else None
