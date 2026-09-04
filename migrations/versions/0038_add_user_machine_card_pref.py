"""Картки верстатів у Налаштуваннях: портрет чи живий кадр — на акаунті.

Revision ID: 0038_add_user_machine_card_pref
Revises: 0037_machine_collect_calibration

Розділ Налаштування → Верстати переїхав з таблиці на модулі-картки. Власник
обрав «Портрет верстата» дефолтом, «Живий кадр» лишив вибором оператора.
Порожній рядок = дефолт, тому наявні акаунти бачать рівно те, що обрано.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0038_add_user_machine_card_pref"
down_revision: str | None = "0037_machine_collect_calibration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ui_machine_card", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("users", "ui_machine_card")
