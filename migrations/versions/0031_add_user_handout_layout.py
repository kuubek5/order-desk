"""Розкладка екрана видачі на акаунті оператора.

Revision ID: 0031_add_user_handout_layout
Revises: 0030_add_user_queue_look_prefs

Порожній рядок — та сама розкладка, що була (один стовпець карток), тому
наявні оператори не побачать зміни, доки самі не перемкнуть.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0031_add_user_handout_layout"
down_revision: str | None = "0030_add_user_queue_look_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("handout_layout", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("users", "handout_layout")
