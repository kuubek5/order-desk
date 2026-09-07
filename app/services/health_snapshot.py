"""Знімок стану бази до оновлення і звірка після нього.

Навіщо. Оновлення застосунку тягне за собою міграції схеми: вони додають
колонки, іноді переписують дані. Досі єдиним доказом «усе на місці» було
відчуття оператора — база могла втратити рядки, і побачив би це той, кому
конкретна робота знадобилась би через тиждень. Тепер застосунок сам рахує,
скільки чого було ДО встановлення оновлення, і після першого старту нової
версії звіряє. Розходження показується на екрані «Стан системи» червоним, з
переліком таблиць.

Як це працює.

1. Перед запуском інсталятора (`POST /settings/update/install`) ми пишемо
   знімок у налаштування — ключ `health_snapshot_before`.
2. На старті застосунку, якщо знімок є і його версія НЕ дорівнює поточній,
   рахуємо ті самі числа ще раз, складаємо звіт і кладемо його в
   `health_snapshot_report`. Знімок «до» прибираємо, щоб звірка не повторилась.
3. Екран «Стан системи» показує останній звіт.

Що вважається втратою. Тільки ЗМЕНШЕННЯ кількості рядків у таблиці. Ріст —
нормальна робота (синк додав роботи, оператор написав коментар). Виняток —
таблиці, які застосунок сам підчищає за розкладом; вони перелічені в
`SELF_PRUNING`, і для них зменшення не вважається втратою.

Знімок навмисно дешевий: `COUNT(*)` по кожній таблиці, жодних даних. Він
переживає перезапис бази інсталятором, бо лежить у самій базі, яку інсталятор
не чіпає (дані живуть окремо від програми — CLAUDE.md §6).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

import app.models  # noqa: F401 — реєструє всі таблиці в metadata
from app.__version__ import VERSION
from app.db import Base
from app.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

BEFORE_KEY = "health_snapshot_before"
REPORT_KEY = "health_snapshot_report"

# Таблиці, які застосунок сам чистить за розкладом: їхнє зменшення — робота
# прибиральника, а не втрата. Решта таблиць зменшуватись не має.
SELF_PRUNING = frozenset({
    "furnace_readings",   # 30 днів історії показань (services/furnace.prune_readings)
    "sync_logs",          # журнал синку з власною межею
    "action_log",         # журнал дій, вікно скасування
    "shift_note_images",  # 180 днів скріншотів зміни
})


# Опорні числа для екрана: таблиця → (одна, дві, пʼять). Оператор упізнає їх
# очима, тому відмінок має бути правильний, а не «1 верстатів».
_TOTALS_WORDS = {
    "orders": ("робота", "роботи", "робіт"),
    "status_events": ("запис історії", "записи історії", "записів історії"),
    "clients": ("клієнт", "клієнти", "клієнтів"),
    "vyrobitok_cells": ("клітинка Виробітку", "клітинки Виробітку", "клітинок Виробітку"),
    "machines": ("верстат", "верстати", "верстатів"),
    "furnaces": ("піч", "печі", "печей"),
}


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    tail = n % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def capture(db: Session) -> dict:
    """Скільки рядків у кожній таблиці просто зараз."""
    tables: dict[str, int] = {}
    for table in Base.metadata.tables:
        try:
            tables[table] = int(db.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0)
        except Exception:  # noqa: BLE001 — таблиці може ще не бути на старій схемі
            logger.debug("Не порахували таблицю %s для знімка", table, exc_info=True)
    return {"version": VERSION, "at": datetime.now().isoformat(timespec="seconds"), "tables": tables}


def remember_before_update(db: Session) -> None:
    """Запамʼятати стан ПЕРЕД встановленням оновлення. Не комітить."""
    set_setting(db, BEFORE_KEY, json.dumps(capture(db), ensure_ascii=False))


def _stored(db: Session, key: str) -> dict | None:
    raw = get_setting(db, key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def compare(before: dict, after: dict) -> dict:
    """Звіт: що зникло, що зʼявилось, чи все гаразд."""
    old = before.get("tables") or {}
    new = after.get("tables") or {}
    lost = {
        name: [count, new.get(name, 0)]
        for name, count in old.items()
        if name not in SELF_PRUNING and new.get(name, 0) < count
    }
    return {
        "ok": not lost,
        "from_version": before.get("version", "?"),
        "to_version": after.get("version", VERSION),
        "checked_at": after.get("at"),
        "lost": lost,
        # Кілька опорних чисел для екрана: їх оператор упізнає очима.
        "totals": {name: new.get(name, 0) for name in _TOTALS_WORDS if name in new},
        "totals_text": " · ".join(
            f"{new[name]} {_plural(new[name], *words)}"
            for name, words in _TOTALS_WORDS.items()
            if name in new
        ),
    }


def check_after_update(db: Session) -> dict | None:
    """Звірити базу зі знімком «до оновлення», якщо версія змінилась.

    Повертає звіт, якщо звірка відбулась, інакше None. Знімок «до» лишається
    на місці, поки версія та сама: оновлення могло не встановитись.
    Не комітить — коміт за викликачем.
    """
    before = _stored(db, BEFORE_KEY)
    if not before:
        return None
    if before.get("version") == VERSION:
        return None  # ще та сама збірка — оновлення не доїхало

    report = compare(before, capture(db))
    set_setting(db, REPORT_KEY, json.dumps(report, ensure_ascii=False))
    set_setting(db, BEFORE_KEY, "")
    if report["ok"]:
        logger.info(
            "Оновлення %s → %s: усе на місці", report["from_version"], report["to_version"]
        )
    else:
        logger.error(
            "Оновлення %s → %s: ЗНИКЛИ рядки: %s",
            report["from_version"], report["to_version"], report["lost"],
        )
    return report


def last_report(db: Session) -> dict | None:
    """Останній звіт звірки для екрана «Стан системи»."""
    return _stored(db, REPORT_KEY)
