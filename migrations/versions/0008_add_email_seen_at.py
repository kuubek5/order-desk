"""Add email_messages.seen_at (triage: unread-by-operator highlight).

Revision ID: 0008_add_email_seen_at
Revises: 0007_add_order_archived_at

NULL = no operator has opened the letter's triage card yet → the row is
highlighted (animated) in the "Нові з пошти" list as "not read by me". A
timestamp is stamped the first time any operator opens the detail panel
(GET /mail/{id}); the state is shared, not per-user (max two operators, a
single seen flag is enough — see CLAUDE.md screen 2). Every pre-existing row
starts NULL: letters already in the queue simply show as unread once, which is
harmless and self-heals on the first open.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_add_email_seen_at"
down_revision: str | None = "0007_add_order_archived_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.add_column(
            sa.Column("seen_at", sa.DateTime(timezone=False), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.drop_column("seen_at")
