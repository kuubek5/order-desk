"""Лист покинув Вхідні пошти — CRM-черга дзеркалить Вхідні.

Revision ID: 0073_email_inbox_gone
Revises: 0072_email_message_id

Усі (адміни, логісти, оператори) чистять скриньку напряму — фільонням у будь-яку
папку й видаленням. CRM «Нові з пошти» мусить це дзеркалити: лист, якого вже нема
у Вхідних, виходить із черги тріажу й живе у вкладці «Покинули Вхідні». Мітка
ставиться синком за фактом «зник із ПОВНОЇ вибірки Вхідних» (не за Message-ID —
щоб ловити й старі листи без нього), знімається при поверненні. Колонка
nullable + index (за нею — вкладка й фільтр черги); NULL для наявних листів.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0073_email_inbox_gone"
down_revision: str | None = "0072_email_message_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_messages", sa.Column("inbox_gone_at", sa.DateTime(), nullable=True)
    )
    op.create_index(
        "ix_email_messages_inbox_gone_at", "email_messages", ["inbox_gone_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_email_messages_inbox_gone_at", table_name="email_messages")
    op.drop_column("email_messages", "inbox_gone_at")
