"""Глушник на відому причину в розділі «Що не так».

Пара «пристрій + причина» (`Problem.key`) тимчасово не показується й не
рахується в значку рейки. Строк обовʼязковий: вічний глушник — це той самий
сліпий екран, тільки тихий. Дивись `WhatsWrongMute` і services/whats_wrong.py.
Таблиця створюється порожньою — рішення заводить людина на екрані.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0069_whats_wrong_mutes"
down_revision: str | None = "0068_folder_merges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whats_wrong_mutes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("note", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("until", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("key", name="uq_whats_wrong_mute_key"),
    )
    op.create_index("ix_whats_wrong_mutes_key", "whats_wrong_mutes", ["key"])


def downgrade() -> None:
    op.drop_index("ix_whats_wrong_mutes_key", table_name="whats_wrong_mutes")
    op.drop_table("whats_wrong_mutes")
