"""Рішення людини про факти з листів клієнта: закріпити або прибрати.

Revision ID: 0098_client_fact_decisions
Revises: 0097_stats_forecast

Самі факти не зберігаються: вони щоразу витягуються з листів. Тут лише те, що людина вирішила
про конкретне значення, щоб наступний лист не повертав прибраний номер.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098_client_fact_decisions"
down_revision: str | None = "0097_stats_forecast"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_fact_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("value_key", sa.String(length=300), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=False), nullable=True),
        sa.UniqueConstraint("client_id", "kind", "value_key", name="uq_client_fact_decision"),
    )
    op.create_index("ix_client_fact_decisions_client_id", "client_fact_decisions", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_client_fact_decisions_client_id", table_name="client_fact_decisions")
    op.drop_table("client_fact_decisions")
