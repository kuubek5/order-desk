"""Multi-order-per-email support (partial accept of multi-colour letters).

Revision ID: 0012_add_partial_accept_links
Revises: 0011_add_client_sender_memory

orders.source_email_id  → the triage letter an email-order came from (one
                          letter can spawn several orders, one per colour batch).
orders.auto_accepted     → future auto-list marker.
attachments.order_id     → which order batch claimed this file; NULL = still
                          unclaimed (in spool), so the letter has more to accept.
All nullable/defaulted — purely additive, existing rows unaffected.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_add_partial_accept_links"
down_revision: str | None = "0011_add_client_sender_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("source_email_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("auto_accepted", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.create_index("ix_orders_source_email_id", ["source_email_id"])
    with op.batch_alter_table("attachments") as batch:
        batch.add_column(sa.Column("order_id", sa.Integer(), nullable=True))
        batch.create_index("ix_attachments_order_id", ["order_id"])


def downgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.drop_index("ix_attachments_order_id")
        batch.drop_column("order_id")
    with op.batch_alter_table("orders") as batch:
        batch.drop_index("ix_orders_source_email_id")
        batch.drop_column("auto_accepted")
        batch.drop_column("source_email_id")
