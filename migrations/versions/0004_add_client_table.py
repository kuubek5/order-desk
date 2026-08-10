"""Add clients table (client profiles — contact info, notes).

Revision ID: 0004_add_client_table
Revises: 0003_add_email_attachments_status

Purely additive: a new standalone `clients` table with no foreign key to
`orders`. Order.client_name stays untouched free text — a Client's orders
are found at read time by fuzzy-matching (see app/client_profile.py), not
by a stored relational link. See that module's docstring and the PR
description for the full reasoning.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_add_client_table"
down_revision: str | None = "0003_add_email_attachments_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("canonical_name", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("email", sa.String(length=300), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("clients")
