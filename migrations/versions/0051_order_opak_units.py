"""Кількість опаків числом: orders.opak_units.

Revision ID: 0051_order_opak_units
Revises: 0050_user_session_epoch

Опак тарифікується окремо, але власної колонки в Google-таблиці не має — його
пишуть у коментар для CAM («2 opaq»). Щоб опаки можна було підсумувати, той
самий факт зберігається ще й числом.

Наявні рядки лишаються з NULL: «у рядку про опак нічого не сказано». Числа
проставляться самі під час наступного синку, бо значення виводиться з
коментаря (app/services/opak.py), а синк перечитує коментарі щоразу.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_order_opak_units"
down_revision: str | None = "0050_user_session_epoch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "orders"
_COLUMN = "opak_units"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
