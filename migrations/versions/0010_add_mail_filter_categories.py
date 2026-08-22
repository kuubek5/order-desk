"""Editable mail-filter categories, seeded with the four defaults.

Revision ID: 0010_add_mail_filter_categories
Revises: 0009_add_mail_filter_rules

The category names used by filter rules / the manual «У фільтр» select were
hardcoded in templates; admins now manage them on the settings screen, so they
live in their own tiny table. Seeded with the same four values the templates
shipped with, so nothing changes visually until someone edits the list.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_add_mail_filter_categories"
down_revision: str | None = "0009_add_mail_filter_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULTS = ("3D-друк", "бухгалтерія", "спам", "інше")


def upgrade() -> None:
    table = op.create_table(
        "mail_filter_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.bulk_insert(table, [{"name": name} for name in _DEFAULTS])


def downgrade() -> None:
    op.drop_table("mail_filter_categories")
