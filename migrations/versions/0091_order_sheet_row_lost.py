"""Робота, чийого рядка в таблиці немає: orders.sheet_row_lost_at.

Revision ID: 0091_order_sheet_row_lost
Revises: 0090_orders_sheet_tab_index

Привід — #6472 (20.10.26): рядок поштової роботи свідомо видалили, синк
поштові роботи не архівує, і фоновий повтор заливки молотив вічно. Синк тепер
ставить позначку, коли рядка немає кілька проходів поспіль, повтор її
оминає, а банер пропонує людині вирішити. Наявні бази не заповнюються:
позначку поставить перший же синк, якщо рядка справді немає.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0091_order_sheet_row_lost"
down_revision: str | None = "0090_orders_sheet_tab_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "orders"
_COLUMN = "sheet_row_lost_at"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.DateTime(timezone=False), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
