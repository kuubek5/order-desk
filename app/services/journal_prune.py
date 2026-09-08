"""Прибирання журналів за строком: `sync_logs` і `action_log`.

Навіщо. Обидві таблиці оголошені в `health_snapshot.SELF_PRUNING` — тобто
застосунок стверджує, що чистить їх за розкладом, — але прибиральника для них
не існувало ЖОДНОГО (аудит 08.09.26). Наслідків два, і другий гірший:

1. Вони ростуть без межі. Журнал синку поповнюється на КОЖЕН запис у таблицю,
   тобто на кожну галочку оператора; за роки це мільйони рядків, які
   сповільнюють усе, що їх читає.
2. Вони — сліпа зона звірки після оновлення. `SELF_PRUNING` означає «зменшення
   тут нормальне», тож справжня втрата даних у цих таблицях не підняла б
   тривоги. Обіцянка без виконавця зробила запобіжник глухим саме там, де він
   мав би дивитись уважно.

Тепер обіцянка виконується, і `SELF_PRUNING` стало правдою.

ЧОМУ ДВА РІЗНІ ВІДЛІКИ ЧАСУ. Це не недбалість, а наслідок реального розходження
в моделях: `ActionLog.created_at` пише ЛОКАЛЬНИЙ час (`default=datetime.now`), а
`SyncLog.occurred_at` — `server_default=func.now()`, тобто на SQLite час за
Гринвічем. Різниця три години. Для вікна в місяці вона нічого не змінює, але
рахувати межу однаково для обох було б просто неправильно, і перший, хто
зменшить вікно до кількох годин, отримав би тихий зсув.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.business_day import utc_now
from app.models import ActionLog, SyncLog

logger = logging.getLogger(__name__)

# Скільки тримати журнал синку. Він діагностичний: відповідає на питання «чому
# ця робота не доїхала в таблицю» і має сенс, поки про той день ще памʼятають.
# 90 днів — утричі довше за вікно черги (30), тож розбір давнього випадку
# лишається можливим.
SYNC_LOG_RETENTION_DAYS = 90

# Журнал дій оператора живе довше: на ньому «хто що зробив», а брак і переробки
# впливають на гроші (CLAUDE.md §5). Рік — щоб перекрити будь-який розбір
# поліття, і все одно скінченно.
ACTION_LOG_RETENTION_DAYS = 365


def prune_journals(
    db: Session,
    *,
    now: datetime | None = None,
    sync_days: int = SYNC_LOG_RETENTION_DAYS,
    action_days: int = ACTION_LOG_RETENTION_DAYS,
) -> dict[str, int]:
    """Прибрати застарілі рядки обох журналів. Повертає {таблиця: скільки}.

    Не комітить: викликач вирішує, коли фіксувати. Помилка не гаситься — тік
    воркера має її побачити й записати, бо мовчазний прибиральник нічим не
    кращий за відсутнього.
    """
    local_now = now or datetime.now()
    # `SyncLog.occurred_at` — за Гринвічем (див. докстрінг модуля), тож і межу
    # для нього рахуємо в тій самій шкалі.
    sync_cutoff = (utc_now() if now is None else now) - timedelta(days=sync_days)
    action_cutoff = local_now - timedelta(days=action_days)

    removed = {
        "sync_logs": db.execute(
            delete(SyncLog).where(SyncLog.occurred_at < sync_cutoff)
        ).rowcount or 0,
        "action_log": db.execute(
            delete(ActionLog).where(ActionLog.created_at < action_cutoff)
        ).rowcount or 0,
    }
    if any(removed.values()):
        logger.info(
            "Журнали прибрано: sync_logs %d, action_log %d",
            removed["sync_logs"], removed["action_log"],
        )
    return removed
