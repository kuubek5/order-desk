"""Mark orders whose sheet row a technician edited after import.

Revision ID: 0018_add_sheet_change_flag
Revises: 0017_seed_modelling_category

A technician who mistypes a colour or drops the wrong folder fixes the row in
the sheet afterwards. The corrected row looks exactly like the old one on
screen, so the operator can mill the version they read minutes earlier — that
is scrap, and it is paid for. These two columns let the queue SHOW that a row
changed: when (``sheet_changed_at``) and what (``sheet_changed_fields``, a
short human list like "колір, шлях"). Both go back to NULL once the operator
dismisses the mark — by their decision, not on a timer, so a change noticed
after a break is still there.

``updated_at`` cannot serve this: it moves on every write, including the
portal's own Sum3D/status write-backs, so it cannot distinguish "someone else
corrected this work" from "we just saved something".
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0018_add_sheet_change_flag"
down_revision: str | None = "0017_seed_modelling_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("sheet_changed_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column("sheet_changed_fields", sa.String(length=400), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("sheet_changed_fields")
        batch.drop_column("sheet_changed_at")
