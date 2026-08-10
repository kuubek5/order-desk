"""Add email_messages.service_type_guess (screen 2 mail triage hint).

Revision ID: 0002_add_email_service_type_guess
Revises: 0001_initial
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_add_email_service_type_guess"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.add_column(sa.Column("service_type_guess", sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.drop_column("service_type_guess")
