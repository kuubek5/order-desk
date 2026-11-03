"""Висота конструкції з STL: orders.stl_height_mm, orders.stl_height_sig.

Revision ID: 0094_order_stl_height
Revises: 0093_user_hide_hold

Ідея №29 (ROADMAP_IDEAS.md, власник 03.11.26): товщина, яку покаже Sum3D,
у рядку черги ще до відкриття CAM. Обидва поля nullable — наявні роботи
просто ще не виміряні.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0094_order_stl_height"
down_revision: str | None = "0093_user_hide_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "orders"
_COLUMNS = (
    ("stl_height_mm", sa.Float()),
    ("stl_height_sig", sa.String(length=500)),
)


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    have = _existing(op.get_bind())
    for name, kind in _COLUMNS:
        if name not in have:
            op.add_column(_TABLE, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    have = _existing(op.get_bind())
    for name, _kind in _COLUMNS:
        if name in have:
            op.drop_column(_TABLE, name)
