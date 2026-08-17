"""Add orders.archived_at (retention: keep-not-delete, Archive screen).

Revision ID: 0007_add_order_archived_at
Revises: 0006_material_catalog_updates

NULL = active. A timestamp means the order left the working queue but is kept
for the archive — it vanished from Google (a whole dated tab or a single row
removed) or was explicitly archived. Orders are no longer hard-deleted on such
removals; ageing out of the retention window is derived from the sheet-tab date
and needs no value here. Every pre-existing row starts active (NULL): the ones
that are genuinely old still show only in the archive because the queue filters
by tab date, so no backfill is required.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_add_order_archived_at"
down_revision: str | None = "0006_material_catalog_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=False), nullable=True)
        )
        batch_op.create_index("ix_orders_archived_at", ["archived_at"])


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_index("ix_orders_archived_at")
        batch_op.drop_column("archived_at")
