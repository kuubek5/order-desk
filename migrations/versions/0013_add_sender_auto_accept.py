"""Trusted-sender auto-accept flag on client_sender_memory.

Revision ID: 0013_add_sender_auto_accept
Revises: 0012_add_partial_accept_links

True = letters from this sender are auto-accepted on arrival when the guardrails
pass (single confident material, no files behind links). Default False → nothing
changes for existing senders until the operator trusts one.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0013_add_sender_auto_accept"
down_revision: str | None = "0012_add_partial_accept_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("client_sender_memory") as batch:
        batch.add_column(
            sa.Column("auto_accept", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("client_sender_memory") as batch:
        batch.drop_column("auto_accept")
