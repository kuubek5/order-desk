"""Порядок списку видачі на акаунті: users.handout_flow.

Revision ID: 0052_user_handout_flow
Revises: 0051_order_opak_units

Видача групує роботи по клієнтах. Але частину дня оператор веде видачу за
самою Google-таблицею, згори вниз, і тоді йому потрібен ТОЙ САМИЙ порядок
рядків, а не картки (прохання власника 07.09.26).

Вибір живе на акаунті, як розкладка й теми: він їде за оператором, а не за
браузером. Порожнє значення — картки по клієнтах, тобто теперішня поведінка.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_user_handout_flow"
down_revision: str | None = "0051_order_opak_units"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "handout_flow"


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
