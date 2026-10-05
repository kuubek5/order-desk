"""Поштова робота чекає свій рядок у таблиці: orders.sheet_row_pending.

Revision ID: 0085_order_sheet_row_pending
Revises: 0084_order_emails

Бриф запобіжників мультипрорахунку, п.1 (05.10.26): рядок-нотатка листа, що не
ліг у таблицю (мережа, квота), більше ніким не дописувався, а загублена
відповідь Google давала дубль — синк заводив із рядка другу роботу. Позначка
каже повтору, кого дописати, а синку — кому віддати «нічий» рядок.

Наявні бази не заповнюються: старі поштові роботи без рядка (до нотаток, без
вкладки) не мають отримати рядок заднім числом.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085_order_sheet_row_pending"
down_revision: str | None = "0084_order_emails"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "orders"
_COLUMN = "sheet_row_pending"


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
