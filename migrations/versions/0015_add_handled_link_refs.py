"""email_messages.handled_link_refs — track which body links were downloaded.

Revision ID: 0015_add_handled_link_refs
Revises: 0014_add_attachment_staged

JSON list of link refs (file_id/url) already pulled, so the «Файли + STL» tab
counts only links STILL to download and the warning clears once all are fetched.
"""

from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0015_add_handled_link_refs"
down_revision: str | None = "0014_add_attachment_staged"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("email_messages") as batch:
        batch.add_column(sa.Column("handled_link_refs", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("email_messages") as batch:
        batch.drop_column("handled_link_refs")
