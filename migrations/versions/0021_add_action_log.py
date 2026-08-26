"""Operator action log — backbone for Undo and the laconic action journal.

Revision ID: 0021_add_action_log
Revises: 0020_add_user_sheet_initial

One row per state-changing operator action (Sum3D, status, comment, handout,
delete, manual add, undo). Powers two features off one table: "Скасувати" reads
the last row to restore field/old_value; the journal renders the note line. See
app/models.py::ActionLog.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0021_add_action_log"
down_revision: str | None = "0020_add_user_sheet_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("field", sa.String(length=50), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_action_log_order_id", "action_log", ["order_id"])
    op.create_index("ix_action_log_operator_id", "action_log", ["operator_id"])
    op.create_index("ix_action_log_action_type", "action_log", ["action_type"])
    op.create_index("ix_action_log_created_at", "action_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_action_log_created_at", table_name="action_log")
    op.drop_index("ix_action_log_action_type", table_name="action_log")
    op.drop_index("ix_action_log_operator_id", table_name="action_log")
    op.drop_index("ix_action_log_order_id", table_name="action_log")
    op.drop_table("action_log")
