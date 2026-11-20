"""Доступ оператора до функції, яким керує адмін (власник 20.11.26).

Три стани на людину й на функцію: «заборонено» (ні перемикача, ні
повідомлень), «на вибір оператора» (власний перемикач у Кабінеті, як було) і
«завжди увімкнено» (оператор не вимкне). Порожній рядок у базі = на вибір
оператора: наявні акаунти після міграції поводяться рівно як до неї.

Функції — у `FEATURES`: колонка таблиці «Оператори», поле доступу й власний
перемикач людини. Нова функція = рядок тут + два стовпці в `users` (міграція).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

CHOICE = ""
DENY = "deny"
FORCE = "force"

# Слово у формі → значення в базі. Кожен стан — ВЛАСНЕ слово: порожнє поле
# сервер читає як «поля не було» (CLAUDE.md §14).
WORDS = {"deny": DENY, "choice": CHOICE, "force": FORCE}
LABELS = {DENY: "Заборонено", CHOICE: "На вибір оператора", FORCE: "Завжди увімкнено"}


@dataclass(frozen=True)
class Feature:
    key: str          # у формі адміна
    title: str        # заголовок колонки
    hint: str         # підказка заголовка
    access_field: str  # поле доступу в `users`
    own_field: str     # власний перемикач людини в `users`


FEATURES: tuple[Feature, ...] = (
    Feature("material_guard", "Сторож матеріалу",
            "Червона картка, коли фрезерується не той матеріал чи колір",
            "material_guard_access", "material_guard"),
    Feature("furnace_power", "Піч без зв'язку",
            "Повідомлення, коли піч перестала відповідати під час спікання",
            "furnace_power_access", "furnace_power_alert"),
    # Кнопка «Написати нам» у кутку екрана (власник 20.11.26: «потрібно мати
    # змогу відключити»). На відміну від двох вище, власний перемикач за
    # замовчуванням УВІМКНЕНИЙ — кнопка була в усіх і лишається, доки не вимкнуть.
    Feature("feedback", "Зворотний зв'язок",
            "Кнопка «Написати нам» (баг, ідея, питання) у кутку екрана",
            "feedback_access", "feedback_beacon"),
)
BY_KEY = {f.key: f for f in FEATURES}


def access_of(user: Optional[Any], feature: Feature) -> str:
    value = (getattr(user, feature.access_field, "") or "") if user is not None else ""
    return value if value in LABELS else CHOICE


def is_on(user: Optional[Any], feature: Feature) -> bool:
    """Чи діє функція для цієї людини: доступ адміна поверх її перемикача."""
    if user is None:
        return False
    access = access_of(user, feature)
    if access == DENY:
        return False
    return access == FORCE or bool(getattr(user, feature.own_field, False))


def own_switch_allowed(user: Optional[Any], feature: Feature, value: bool) -> bool:
    """Чи може людина сама поставити свій перемикач у `value`."""
    access = access_of(user, feature)
    if access == DENY:
        return False
    return not (access == FORCE and not value)
