"""Два моменти для статистики: orders.row_seen_at і orders.job_code_at.

Revision ID: 0089_order_stats_moments
Revises: 0088_user_seen_hello_day

STATS_BRIEF.md §3, етап 0 — почати ЗАПИСУВАТИ, поки екрана ще немає: кожен
день затримки = день історії, якої вже не буде. `row_seen_at` — коли рядок
зʼявився в таблиці (ставить живий тік синку поточної вкладки й ручне
додавання), `job_code_at` — коли технік уперше вписав «Номер роботи» (шлях).
Обидва — київський місцевий час без пояса; NULL = момент невідомий (уся
історія до цієї міграції, імпорт історії, воскресіння, перевикористаний
рядок). Індекс поки не потрібен — читатиме лише майбутній екран статистики.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089_order_stats_moments"
down_revision: str | None = "0088_user_seen_hello_day"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "orders"
_COLUMNS = ("row_seen_at", "job_code_at")


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    have = _existing(op.get_bind())
    for name in _COLUMNS:
        if name not in have:
            op.add_column(_TABLE, sa.Column(name, sa.DateTime(timezone=False), nullable=True))


def downgrade() -> None:
    have = _existing(op.get_bind())
    for name in _COLUMNS:
        if name in have:
            op.drop_column(_TABLE, name)
