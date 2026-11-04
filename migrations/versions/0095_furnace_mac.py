"""MAC печі: furnaces.mac_address.

Revision ID: 0095_furnace_mac
Revises: 0094_order_stl_height

Власник 04.11.26: після вимкнення світла піч лишилась без зв'язку, доступу до
роутера немає. `furnace_rediscover` знаходить піч за MAC і сам переписує адресу.
Nullable — наявні печі запам'ятають MAC, щойно прочитаються.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0095_furnace_mac"
down_revision: str | None = "0094_order_stl_height"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "furnaces"
_COLUMN = "mac_address"


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _existing(op.get_bind()):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=17), nullable=True))


def downgrade() -> None:
    if _COLUMN in _existing(op.get_bind()):
        op.drop_column(_TABLE, _COLUMN)
