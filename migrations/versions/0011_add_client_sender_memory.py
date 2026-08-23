"""Recurring-client memory keyed by sender address (accept wizard autofill).

Revision ID: 0011_add_client_sender_memory
Revises: 0010_add_mail_filter_categories

One row per sender key (from_address, plus the quoted original sender for
forwarded mail): the client name the operator typed and the export folder the
files went to on the latest accept, with a running count. Empty on upgrade —
fills itself from the next accept onward.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_add_client_sender_memory"
down_revision: str | None = "0010_add_mail_filter_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_sender_memory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sender_key", sa.String(400), nullable=False),
        sa.Column("client_name", sa.String(200), nullable=False),
        sa.Column("export_folder", sa.String(200), nullable=True),
        sa.Column("orders_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_seen_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_client_sender_memory_sender_key",
        "client_sender_memory",
        ["sender_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_client_sender_memory_sender_key", table_name="client_sender_memory")
    op.drop_table("client_sender_memory")
