"""Пряме посилання теки видачі для поштової роботи.

Revision ID: 0074_order_export_folder_path
Revises: 0073_email_inbox_gone

Прийняття листа саме переносить файли в export/<клієнт>/<партія>/<матеріал> і
знає фінальну теку точно. Замість нечіткого збігу за іменем клієнта на видачі
(який плутається, коли в клієнта кілька робіт того самого кольору) зберігаємо
REL-шлях цієї теки прямо на роботі — видача бере STL звідти напряму. Колонка
nullable; NULL для наявних робіт і для всіх НЕпоштових (лаба, вписаний клієнт).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0074_order_export_folder_path"
down_revision: str | None = "0073_email_inbox_gone"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders", sa.Column("export_folder_path", sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "export_folder_path")
