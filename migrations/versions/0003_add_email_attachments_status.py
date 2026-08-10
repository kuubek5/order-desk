"""Add email_messages.attachments_status (two-phase mail fetch, screen 2).

Revision ID: 0003_add_email_attachments_status
Revises: 0002_add_email_service_type_guess

Every pre-existing EmailMessage row went through the OLD single-phase
fetch_new_emails, which only ever committed a row once its body and
attachments were already fully saved — so every row that existed before
this migration has already-complete attachment processing, regardless of
whether it ended up with zero or several Attachment rows. Those rows are
backfilled to "ready" here. Only rows created by the new two-phase fetch
(phase 1, headers-only) start out as "pending" — via the ORM-level default
declared on the model, applied to rows created after this migration ships.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_add_email_attachments_status"
down_revision: str | None = "0002_add_email_service_type_guess"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.add_column(
            sa.Column("attachments_status", sa.String(length=20), nullable=True)
        )

    # Backfill: every row that existed before this migration already has its
    # attachment processing complete (see module docstring) — mark it
    # "ready", not the new-row default "pending", or every historical email
    # would show a permanent false "downloading..." badge.
    email_messages = sa.table(
        "email_messages", sa.column("attachments_status", sa.String(length=20))
    )
    op.execute(email_messages.update().values(attachments_status="ready"))

    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.alter_column("attachments_status", nullable=False, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.drop_column("attachments_status")
