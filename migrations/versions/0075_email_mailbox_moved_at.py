"""Час переносу листа в папку «оброблено» — вкладка показує лише сьогодні.

Revision ID: 0075_email_mailbox_moved_at
Revises: 0074_order_export_folder_path

Вкладка «Оброблено» (папка «Скачано-прошитано») має показувати лише листи
поточного робочого дня (власник 24.09.26), інакше вона росте безмежно. Для цього
треба знати, КОЛИ лист потрапив у папку. Колонка nullable; NULL для наявних
перенесених листів (вони не сьогоднішні — просто не показуються за сьогодні).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0075_email_mailbox_moved_at"
down_revision: str | None = "0074_order_export_folder_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_messages", sa.Column("mailbox_moved_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("email_messages", "mailbox_moved_at")
