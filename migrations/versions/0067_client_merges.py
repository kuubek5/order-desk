"""Рішення власника по парах схожих клієнтів: злити або «не дублі».

Зводить різні написання одного клієнта до канону в підказках і на екрані
клієнтів. Видачу НЕ чіпає. Дивись `ClientMerge` і services/client_merge.py.
Таблиця створюється порожньою — рішення заводить власник на екрані.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0067_client_merges"
down_revision: str | None = "0066_material_shortcuts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_merges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("a_key", sa.String(length=200), nullable=False),
        sa.Column("b_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("variant_key", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("variant_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("canonical_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("a_key", "b_key", name="uq_client_merge_pair"),
    )
    op.create_index("ix_client_merges_a_key", "client_merges", ["a_key"])
    op.create_index("ix_client_merges_b_key", "client_merges", ["b_key"])


def downgrade() -> None:
    op.drop_index("ix_client_merges_b_key", table_name="client_merges")
    op.drop_index("ix_client_merges_a_key", table_name="client_merges")
    op.drop_table("client_merges")
