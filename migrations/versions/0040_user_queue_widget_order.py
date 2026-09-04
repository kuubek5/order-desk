"""Порядок віджетів черги на акаунті оператора.

Revision ID: 0040_user_queue_widget_order
Revises: 0039_machine_portrait_model

Режим редагування віджетів (шестерня вигляду на черзі) дає перетягувати
смугу верстатів угорі й секції правої панелі. Порядок персональний, тому
на акаунті; порожній рядок = порядок за замовчуванням.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0040_user_queue_widget_order"
down_revision: str | None = "0039_machine_portrait_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (("queue_side_order", 200), ("queue_strip_order", 300))


def upgrade() -> None:
    for name, length in _COLUMNS:
        op.add_column(
            "users",
            sa.Column(name, sa.String(length=length), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("users", name)
