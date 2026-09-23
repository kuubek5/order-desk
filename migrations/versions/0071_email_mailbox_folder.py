"""Папка скриньки, куди переміщено оброблений лист.

Revision ID: 0071_email_mailbox_folder
Revises: 0070_email_from_name

Оператор після запуску роботи в цех вручну переносить лист у папку скриньки
(«Скачано-просчитано»). Колонка тримає назву цієї папки; NULL — лист ще в Inbox.
Ми моніторимо лише Inbox, тож переміщений лист синк не перечитує, а рядок
лишається — поле каже, що лист уже прибрано, й ховає кнопку. NULL для всіх
наявних листів — поведінка та сама, що й досі.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0071_email_mailbox_folder"
down_revision: str | None = "0070_email_from_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("mailbox_folder", sa.String(length=300), nullable=True))


def downgrade() -> None:
    op.drop_column("email_messages", "mailbox_folder")
