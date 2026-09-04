"""Верстати: обраний портрет-модель для картки.

Revision ID: 0039_machine_portrait_model
Revises: 0038_add_user_machine_card_pref

Власник обирає, який із чотирьох згенерованих портретів (350i, 350i loader,
250i, 250i dry) показує картка верстата. Порожній рядок = здогад за назвою,
як було — наявні рядки нічого не помічають.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0039_machine_portrait_model"
down_revision: str | None = "0038_add_user_machine_card_pref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "machines",
        sa.Column("portrait_model", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("machines", "portrait_model")
