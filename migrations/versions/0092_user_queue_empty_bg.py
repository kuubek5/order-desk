"""Фон порожньої черги — вибір оператора: users.queue_empty_bg.

Revision ID: 0092_user_queue_empty_bg
Revises: 0091_order_sheet_row_lost

Власник 28.10.26: кадр порожньої черги має мінятись — під тему або на смак
оператора. Порожній рядок = «за темою» (app/services/queue_empty_bg.py), тож
наявні акаунти отримують фон своєї теми без жодного заповнення.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0092_user_queue_empty_bg"
down_revision: str | None = "0091_order_sheet_row_lost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "queue_empty_bg"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(20), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
