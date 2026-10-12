"""Індекс orders.sheet_tab для «Статистики».

Revision ID: 0090_orders_sheet_tab_index
Revises: 0089_order_stats_moments

STATS_BRIEF.md §7: агрегати рахуються в SQL з фільтром періоду, а період —
це список вкладок (`sheet_tab IN (...)`, app/services/stats_data.py). Без
індексу кожен графік екрана переглядав би всю таблицю робіт (вона росте на
~92 рядки за робочий день і не чиститься).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0090_orders_sheet_tab_index"
down_revision: str | None = "0089_order_stats_moments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ix_orders_sheet_tab"


def _existing(bind) -> set[str]:
    return {ix["name"] for ix in sa.inspect(bind).get_indexes("orders")}


def upgrade() -> None:
    if _NAME not in _existing(op.get_bind()):
        op.create_index(_NAME, "orders", ["sheet_tab"])


def downgrade() -> None:
    if _NAME in _existing(op.get_bind()):
        op.drop_index(_NAME, table_name="orders")
