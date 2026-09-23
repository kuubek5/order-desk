"""Message-ID листа — для повернення з папки в Inbox.

Revision ID: 0072_email_message_id
Revises: 0071_email_mailbox_folder

UID листа міняється при кожному переміщенні між папками, тож повернути
перенесений лист назад у Inbox за UID неможливо. Message-ID стабільний —
знаходимо лист саме за ним. Колонка nullable + index; NULL для наявних листів
(кнопка «у вхідні» для них ховається). Заповнюється при імпорті нових листів і
при переміщенні (беккфіл на льоту, якщо порожнє).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0072_email_message_id"
down_revision: str | None = "0071_email_mailbox_folder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("message_id", sa.String(length=400), nullable=True))
    op.create_index("ix_email_messages_message_id", "email_messages", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_email_messages_message_id", table_name="email_messages")
    op.drop_column("email_messages", "message_id")
