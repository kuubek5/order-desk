"""Кількість одиниць з назв STL (бриф STL_UNITS_BRIEF.md, власник 08.10.26).

Exocad кладе номери зубів (FDI) у назву файлу:
`2026-10-06_00033-001-17-16-15-14-waxup_cad.stl` — зуби 17, 16, 15, 14.
Формула та сама, що в іншому проєкті власника («SLM Writer»,
`count_teeth_in_filename`): токен рахується, лише якщо він рівно з двох цифр і є
кодом постійного зуба (11–18, 21–28, 31–38, 41–48); `00033`, `001`, слова
`waxup`/`crown` відпадають самі.

Рішення власника: рахуються УНІКАЛЬНІ зуби по всіх файлах роботи — той самий
зуб у двох файлах (waxup + коронка 34) — одна одиниця. Повтор показуємо
підказкою, щоб оператор бачив, звідки число. Число — лише підказка в полі з
бейджем «?»: рішення за оператором, і число, яке він ввів сам, не
перезаписується ніколи.

Перевірено на живому (08.10.26): тека `2026-10-06_00033-001` (17-16-15-14,
24-25-26-27, 34, 35, 44-45-46-47) → 14 = кількість роботи 34123 у таблиці.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Iterable

# Лише файли моделей: архів, проєкт Exocad (.dentalProject), .sfv тощо — ні.
MODEL_SUFFIXES = {".stl", ".ply", ".obj", ".3mf"}

# Дата (і час одразу за нею) — не зуби: у `2026-10-07_23-37-14` усі три пари
# часу схожі на коди FDI (23, 37, 14), а в `16.09.2026` — «16».
_DATE_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[ _-]\d{2}-\d{2}-\d{2})?|\d{2}\.\d{2}\.\d{2,4}"
)
# Назва Exocad: `<назва проєкту>-<зуби через дефіс>-<тип>_cad`. Назва проєкту
# буває будь-якою (`16.09.2026-Проєкт юшков вч цр`, зіпсована дата
# `662026-333307-13_…`), тож зуби беремо ЛИШЕ з ряду безпосередньо перед
# `-<тип>_cad` (dev-копія 08.10.26: загальне правило давало там зайві 16 і 13).
_EXOCAD_TEETH = re.compile(r"(?<!\d)((?:\d{2}-)*\d{2})-([A-Za-z]+)_cad", re.IGNORECASE)

# Капа (`bitesplint_cad`) — одна деталь на всю щелепу, і в таблиці вона завжди
# 14 одиниць (власник 08.10.26; 06.10.26 «kappa 14» у трьох клієнтів). Номер
# зуба в назві (`…-41-bitesplint_cad`) каже лише, ЯКА щелепа: 1x/2x — верх,
# 3x/4x — низ. Дві капи (верх і низ) — 28; копія тієї самої капи — ні.
SPLINT_TYPES = {"bitesplint", "splint", "nightguard"}
SPLINT_UNITS = 14
# Як капу називають у цеху й клієнти (власник 08.10.26): сплінт, капа, каппа —
# і латиницею. Окремим словом у назві файлу, а не частиною іншого слова.
_SPLINT_WORD = re.compile(
    r"(?<![^\W\d_])(?:bite\s*splint|splint|night\s*guard|kap{1,2}a|сплінт|сплинт|кап{1,2}а)(?![^\W\d_])",
    re.IGNORECASE,
)


def is_splint_file(filename: str) -> bool:
    """Файл капи: тип Exocad `bitesplint_cad` або слово «капа»/«сплінт»/… у назві."""
    name = PurePath(filename or "").stem
    return exocad_type(filename) in SPLINT_TYPES or bool(_SPLINT_WORD.search(name.replace("_", " ")))
_SPLIT = re.compile(r"[-_\s.]+")


def _is_permanent_tooth(n: int) -> bool:
    return n // 10 in (1, 2, 3, 4) and 1 <= n % 10 <= 8


def teeth_in_filename(filename: str) -> list[int]:
    """Зуби з однієї назви файлу, у порядку появи, без повторів."""
    stem = PurePath(filename or "").name
    suffix = PurePath(stem).suffix
    if suffix:
        stem = stem[: -len(suffix)]
    exocad = list(_EXOCAD_TEETH.finditer(stem))
    tokens = exocad[-1].group(1).split("-") if exocad else _SPLIT.split(_DATE_TIME.sub(" ", stem))
    teeth: list[int] = []
    for token in tokens:
        if len(token) == 2 and token.isdigit():
            n = int(token)
            if _is_permanent_tooth(n) and n not in teeth:
                teeth.append(n)
    return teeth


def exocad_type(filename: str) -> str:
    """Тип роботи з назви Exocad (`crown`, `waxup`, `bitesplint`…) або ""."""
    found = list(_EXOCAD_TEETH.finditer(PurePath(filename or "").name))
    return found[-1].group(2).lower() if found else ""


def is_model_file(filename: str) -> bool:
    return PurePath(filename or "").suffix.lower() in MODEL_SUFFIXES


@dataclass
class UnitsCount:
    units: int = 0
    teeth: list[int] = field(default_factory=list)
    # Зуби, що трапились у кількох файлах (рахуються один раз).
    repeated: list[int] = field(default_factory=list)
    # Файли моделей, у назві яких зубів немає (моделі щелеп, ясна, не Exocad).
    without_teeth: list[str] = field(default_factory=list)
    # Щелепи кап: «верх» / «низ» / «щелепа ?» (номера зуба в назві немає).
    splint_jaws: list[str] = field(default_factory=list)
    model_files: int = 0

    @property
    def summary(self) -> str:
        """Підказка «звідки число»: зуби по квадрантах, повтори окремо."""
        parts = [f"капа {jaw} = {SPLINT_UNITS}" for jaw in self.splint_jaws]
        if not self.teeth:
            return " · ".join(parts)
        for quadrant in (1, 2, 3, 4):
            row = [str(t) for t in self.teeth if t // 10 == quadrant]
            if row:
                parts.append(",".join(row))
        text = " · ".join(parts)
        if self.repeated:
            text += " · у кількох файлах: " + ",".join(str(t) for t in self.repeated)
        return text


def count_units(filenames: Iterable[str]) -> UnitsCount:
    """Унікальні зуби по всіх файлах моделей роботи."""
    result = UnitsCount()
    seen: dict[int, int] = {}
    for name in filenames:
        if not is_model_file(name):
            continue
        result.model_files += 1
        if is_splint_file(name):
            jaw = _splint_jaw(teeth_in_filename(name), name)
            if jaw not in result.splint_jaws:
                result.splint_jaws.append(jaw)
            continue
        teeth = teeth_in_filename(name)
        if not teeth:
            result.without_teeth.append(PurePath(name).name)
            continue
        for tooth in teeth:
            seen[tooth] = seen.get(tooth, 0) + 1
    result.teeth = sorted(seen)
    result.repeated = sorted(t for t, n in seen.items() if n > 1)
    result.units = len(result.teeth) + SPLINT_UNITS * len(result.splint_jaws)
    return result


_UPPER_WORD = re.compile(r"верх|верхн|upper|maxill", re.IGNORECASE)
_LOWER_WORD = re.compile(r"низ|нижн|lower|mandib", re.IGNORECASE)


def _splint_jaw(teeth: list[int], name: str = "") -> str:
    """Щелепа капи: з номера зуба, а без нього — зі слова в назві."""
    quadrants = {t // 10 for t in teeth}
    if quadrants and quadrants <= {1, 2}:
        return "верх"
    if quadrants and quadrants <= {3, 4}:
        return "низ"
    upper, lower = bool(_UPPER_WORD.search(name)), bool(_LOWER_WORD.search(name))
    if upper != lower:
        return "верх" if upper else "низ"
    return "щелепа ?"


def letter_quantity(raw: str | None) -> int | None:
    """Кількість, яку клієнт написав у листі (`quantity_guess`), числом."""
    text = (raw or "").strip()
    return int(text) if text.isdigit() else None
