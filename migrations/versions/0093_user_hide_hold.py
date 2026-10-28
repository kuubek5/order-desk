"""Чіп «на погодженні» — сховати одним кліком: users.queue_hide_hold, mail_dock_hide_hold.

Revision ID: 0093_user_hide_hold
Revises: 0092_user_queue_empty_bg

Власник 28.10.26 (HOLD_FILTER_BRIEF.md, варіант А): дві незалежні кнопки —
черга і док мультипрорахунку на екрані пошти. За замовчуванням вимкнено, тож
наявні акаунти бачать усе, як і раніше.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0093_user_hide_hold"
down_revision: str | None = "0092_user_queue_empty_bg"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMNS = ("queue_hide_hold", "mail_dock_hide_hold")


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    have = _existing(op.get_bind())
    for name in _COLUMNS:
        if name not in have:
            op.add_column(
                _TABLE,
                sa.Column(name, sa.Boolean(), nullable=False, server_default="0"),
            )


def downgrade() -> None:
    have = _existing(op.get_bind())
    for name in _COLUMNS:
        if name in have:
            op.drop_column(_TABLE, name)
