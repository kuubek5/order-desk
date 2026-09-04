# -*- coding: utf-8 -*-
"""Порядок віджетів черги: секції правої панелі й смуга верстатів.

Порядок ЗАСТОСОВУЄ СЕРВЕР (`style="order:N"` на секції, сортування карток
верстатів), бо обидві зони живуть під поллами: смуга свапається кожні 10 с,
секції печей і верстатів — кожні 30 с. Виставлений лише клієнтом порядок
помирав би разом зі старою розміткою через кілька секунд — той самий урок,
що з класами згортання на body.

Збережений список — це ПОБАЖАННЯ, а не істина: секція могла зникнути з
розмітки, верстат — з переліку, а новий з'явитися. Тому обидві функції
працюють як фільтр+доважок: спершу відоме в збереженому порядку, далі решта
в порядку за замовчуванням. Невідомий ключ ігнорується мовчки.
"""

from __future__ import annotations

#: Секції правої панелі в порядку за замовчуванням (ключі `data-sec`).
SIDE_SECTIONS: tuple[str, ...] = ("mail", "furnace", "machine", "sync", "handout")

#: Скільки елементів приймаємо в одному списку — пасок від роздутого поля.
MAX_ITEMS = 40


def parse_order(raw: str | None) -> list[str]:
    """Рядок з акаунта → список ключів без порожніх і дублів."""
    if not raw:
        return []
    seen: list[str] = []
    for chunk in str(raw).split(","):
        key = chunk.strip()
        if key and key not in seen:
            seen.append(key)
    return seen[:MAX_ITEMS]


def clean_side_order(raw: str | None) -> str:
    """Впорядкувати й відсіяти чуже — у полі лишаються тільки наші секції."""
    known = [key for key in parse_order(raw) if key in SIDE_SECTIONS]
    return ",".join(known)


def clean_strip_order(raw: str | None) -> str:
    """Смуга верстатів: лише додатні цілі (id рядків `machines`)."""
    out: list[str] = []
    for key in parse_order(raw):
        if key.isdigit() and key != "0":
            out.append(key)
    return ",".join(out)


def side_index(saved: str | None, section: str) -> int:
    """Номер секції для CSS `order`. Незбережена секція йде після збережених,
    зберігаючи порядок за замовчуванням — інакше нова секція стрибала б на
    початок панелі просто тому, що її ще не перетягували."""
    order = [key for key in parse_order(saved) if key in SIDE_SECTIONS]
    if section in order:
        return order.index(section)
    rest = [key for key in SIDE_SECTIONS if key not in order]
    tail = rest.index(section) if section in rest else len(rest)
    return len(order) + tail


def sort_machine_cards(saved: str | None, cards: list) -> list:
    """Картки верстатів у збереженому порядку; невідомі — у кінці, як були."""
    order = parse_order(saved)
    if not order:
        return list(cards)
    position = {key: index for index, key in enumerate(order)}

    def rank(card) -> tuple[int, int]:
        machine_id = getattr(getattr(card, "target", None), "machine_id", None)
        if machine_id is None:
            return (len(position), 0)
        return (position.get(str(machine_id), len(position)), 0)

    return [card for _, card in sorted(enumerate(cards), key=lambda pair: (rank(pair[1]), pair[0]))]
