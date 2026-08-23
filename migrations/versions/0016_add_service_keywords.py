"""Add service_keywords table (editable mail service-type recogniser).

Revision ID: 0016_add_service_keywords
Revises: 0015_add_handled_link_refs

Creates the ServiceKeyword table and seeds it from app/service_classifier.py's
taxonomy so a fresh install flags 3D-print letters out of the box. The badge
(розпізнано / перевірити) on the triage screen reads these rules; admins extend
them at runtime from /settings/recognition without a code change — same
accumulating-dictionary idea as material_aliases (migration 0005).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from app.service_classifier import SEED_SERVICE_KEYWORDS


revision: str = "0016_add_service_keywords"
down_revision: str | None = "0015_add_handled_link_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_keywords",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False, server_default="contains"),
        sa.Column("service_type", sa.String(length=50), nullable=False, server_default="3d_print"),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_service_keywords_pattern", "service_keywords", ["pattern"])

    bind = op.get_bind()
    keyword_tbl = sa.table(
        "service_keywords",
        sa.column("pattern", sa.String),
        sa.column("match_type", sa.String),
        sa.column("service_type", sa.String),
        sa.column("confirmed", sa.Boolean),
    )
    for service_type, keywords in SEED_SERVICE_KEYWORDS.items():
        for pattern, match_type in keywords:
            bind.execute(
                keyword_tbl.insert().values(
                    pattern=pattern,
                    match_type=match_type,
                    service_type=service_type,
                    confirmed=True,
                )
            )


def downgrade() -> None:
    op.drop_index("ix_service_keywords_pattern", table_name="service_keywords")
    op.drop_table("service_keywords")
