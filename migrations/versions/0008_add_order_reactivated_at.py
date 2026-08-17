"""Add orders.reactivated_at (unarchive: pull an archived work back to the queue).

Revision ID: 0008_add_order_reactivated_at
Revises: 0007_add_order_archived_at

NULL = normal. A timestamp means an operator explicitly pulled this work out of
the Archive back into the working queue, so it stays active even though its
business date is older than the retention window (which would otherwise age it
straight back out). Unarchiving clears archived_at and stamps this; the working
queue and the archive predicate both honour it. No backfill — every existing row
starts NULL (never manually reactivated).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_add_order_reactivated_at"
down_revision: str | None = "0007_add_order_archived_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(
            sa.Column("reactivated_at", sa.DateTime(timezone=False), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("reactivated_at")
