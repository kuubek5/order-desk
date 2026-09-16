"""Рішення власника по парах схожих ТЕК в export: одна папка одного клієнта.

Видача читає теки однієї групи разом, тож робота, розсипана по «Ніколаєв» і
«Іван Ніколаєв», знову збирається в одного клієнта. Файли на диску не рухаємо.
Дивись `FolderMerge` і services/folder_merge.py. Таблиця створюється порожньою.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0068_folder_merges"
down_revision: str | None = "0067_client_merges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "folder_merges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("a_key", sa.String(length=200), nullable=False),
        sa.Column("b_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("a_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("b_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("a_key", "b_key", name="uq_folder_merge_pair"),
    )
    op.create_index("ix_folder_merges_a_key", "folder_merges", ["a_key"])
    op.create_index("ix_folder_merges_b_key", "folder_merges", ["b_key"])


def downgrade() -> None:
    op.drop_index("ix_folder_merges_b_key", table_name="folder_merges")
    op.drop_index("ix_folder_merges_a_key", table_name="folder_merges")
    op.drop_table("folder_merges")
