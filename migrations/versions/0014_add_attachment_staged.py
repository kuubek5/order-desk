"""attachments.staged_to_export — trusted-sender auto-download to export.

Revision ID: 0014_add_attachment_staged
Revises: 0013_add_sender_auto_accept

True = the file was auto-moved into export before acceptance (trusted sender),
so the manual accept links it without moving it again. Default False.
"""

from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0014_add_attachment_staged"
down_revision: str | None = "0013_add_sender_auto_accept"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.add_column(
            sa.Column("staged_to_export", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.drop_column("staged_to_export")
