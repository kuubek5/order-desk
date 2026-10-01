"""Зведення Конвеєра для мультипрорахунку (власник 02.10.26).

Оператор відмічає у «Вхідних» листи-кандидати на один диск Sum3D. Над картками
Конвеєра — один рядок: який матеріал·колір у більшості, скільки листів,
одиниць і клієнтів, і хто з обраних має ІНШИЙ матеріал чи колір (підсвічуємо,
але не забороняємо — рішення за оператором).

Текст складає лише сервер (той самий принцип, що в кошику «Нових дисків»):
браузер шле поточні значення карток, сюди ж приходить і перший рендер.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.material_classifier import match_key

# `моно а 3,5` і `mono a3.5` — один диск: після фолду кирилиці прибираємо
# пробіл між літерою відтінку й цифрою.
_SHADE_GAP = re.compile(r"(?<=[a-z])\s+(?=\d)")


def material_group_key(raw: str | None) -> str:
    """Ключ «той самий матеріал і колір» для зведення. Лише для порівняння —
    показується оператору його власне написання."""
    return _SHADE_GAP.sub("", match_key(raw))


@dataclass
class ConveyorCard:
    email_id: int
    client: str
    material: str
    quantity: str


@dataclass
class ConveyorSummary:
    label: str = ""
    letters: int = 0
    units: int = 0
    units_unknown: int = 0
    clients: int = 0
    total: int = 0
    odd: list[ConveyorCard] = field(default_factory=list)

    @property
    def odd_ids(self) -> set[int]:
        return {c.email_id for c in self.odd}


def _units(raw: str) -> int | None:
    text = (raw or "").strip()
    return int(text) if text.isdigit() else None


def conveyor_summary(cards: list[ConveyorCard]) -> ConveyorSummary:
    """Більшість за матеріалом·кольором; при рівності — та, що вище в списку.

    Картка без матеріалу в більшість не рахується і «іншим» не вважається:
    там просто ще нічого не вписано.
    """
    summary = ConveyorSummary(total=len(cards))
    groups: dict[str, list[ConveyorCard]] = {}
    for card in cards:
        key = material_group_key(card.material)
        if key:
            groups.setdefault(key, []).append(card)
    if not groups:
        return summary
    # dict зберігає порядок першої появи, а max бере перший із рівних.
    main_key = max(groups, key=lambda k: len(groups[k]))
    main = groups[main_key]
    summary.label = " ".join(main[0].material.split())
    summary.letters = len(main)
    for card in main:
        n = _units(card.quantity)
        if n is None:
            summary.units_unknown += 1
        else:
            summary.units += n
    summary.clients = len({" ".join(c.client.lower().split()) for c in main if c.client.strip()})
    summary.odd = [c for k, group in groups.items() if k != main_key for c in group]
    return summary
