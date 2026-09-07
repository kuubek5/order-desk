"""Запобіжник на масові стирання рядків у Google-таблиці.

Стирання рядка чистить A:K у спільній таблиці, яку дивляться логісти,
адміністратори й техніки. Одиничне стирання захищене звіркою позиції
(`_resolve_row`) і слідом у «Журналі синку» — з нього рядок можна набрати
назад. Чого не було досі — стелі на КІЛЬКІСТЬ: цикл у коді, повторний
відкат прийняття або оператор, що клацає видалення підряд, стерли б десятки
рядків, і кожне стирання окремо виглядало б законним.

Запобіжник на масову архівацію в базі (`sync.py`, >5 робіт і >25% за тік)
закриває протилежний бік — зникнення робіт із ЧЕРГИ. Тут дзеркальне
правило для зворотного напрямку: скільки рядків застосунок має право
стерти в САМІЙ таблиці за годину.

Ліміт свідомо не «скільки завгодно, аби підтвердив»: підтвердження в мить
збою натискають не думаючи. Перевищили — стирання пропускається, рядок
лишається в таблиці. Це безпечний бік відмови: зайвий рядок прибирається
руками за секунди, стерта чужа робота не повертається ніяк.

Вікно живе в памʼяті процесу: розігнатись на сотні стирань може лише
запущений застосунок, а рестарт як стеля сам собою вже зупиняє розгін.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=1)


def _limit_from_env() -> int:
    """Стеля стирань за годину. 20 — із запасом на найгустіший законний
    сценарій (відкат прийняття листа, де кілька робіт одного клієнта), але
    на порядок менше за розгін циклу."""
    raw = os.environ.get("KUUBMILL_SHEET_ERASE_LIMIT", "")
    try:
        value = int(raw)
    except ValueError:
        return 20
    return max(1, value)


LIMIT = _limit_from_env()

_erases: deque[datetime] = deque()


class SheetEraseBlocked(RuntimeError):
    """Стирання пропущено запобіжником, а не помилкою таблиці.

    Окремий тип, а не False: викликач має написати в журнал ПРАВИЛЬНУ
    причину — «спрацював запобіжник», а не «рядок не підтверджено».
    """

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(
            f"за останню годину вже стерто рядків: {count} (стеля {limit}) — "
            "стирання зупинено, приберіть рядок у таблиці вручну"
        )


def _trim(now: datetime) -> None:
    edge = now - WINDOW
    while _erases and _erases[0] < edge:
        _erases.popleft()


def check(now: datetime | None = None) -> None:
    """Кинути `SheetEraseBlocked`, якщо стеля вичерпана. Інакше нічого."""
    now = now or datetime.now()
    _trim(now)
    if len(_erases) >= LIMIT:
        logger.error(
            "Запобіжник стирань: за годину вже %s рядків (стеля %s) — стирання зупинено",
            len(_erases), LIMIT,
        )
        raise SheetEraseBlocked(len(_erases), LIMIT)


def record(now: datetime | None = None) -> None:
    """Зафіксувати ЗДІЙСНЕНЕ стирання."""
    now = now or datetime.now()
    _trim(now)
    _erases.append(now)


def recent_count(now: datetime | None = None) -> int:
    now = now or datetime.now()
    _trim(now)
    return len(_erases)


def reset_for_tests() -> None:
    _erases.clear()
