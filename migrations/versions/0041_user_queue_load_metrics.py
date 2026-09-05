"""Які показники стрічки навантаження показувати — на акаунті.

Revision ID: 0041_user_queue_load_metrics
Revises: 0040_user_queue_widget_order

Стрічка навантаження в шапці черги (CRM / ПК / ОЗП) тепер вимикається
по-окремо в шестерні вигляду. CSV із crm/pc/ram; порожньо = вимкнено цілком.
server_default = усі три, тому наявні акаунти бачать те саме, що й раніше.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0041_user_queue_load_metrics"
down_revision: str | None = "0040_user_queue_widget_order"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("queue_load_metrics", sa.String(length=20),
                  nullable=False, server_default="crm,pc,ram"),
    )


def downgrade() -> None:
    op.drop_column("users", "queue_load_metrics")
