"""Оператор повернув лист із «Покинули Вхідні» у Вхідні.

Revision ID: 0077_email_inbox_returned
Revises: 0076_restore_mail_work_source

Синк мітить «покинув Вхідні» кожен лист «нове», старший за вікно синку, не
питаючи скриньку (`_reconcile_inbox_gone`, гілка «за віком»). Лист, який
оператор свідомо повернув у Вхідні кнопкою, злітав би назад за 2 хвилини.
Позначка `inbox_returned_at` каже цій гілці його оминати. Nullable, NULL для
наявних листів — поведінка для них не змінюється.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0077_email_inbox_returned"
down_revision: str | None = "0076_restore_mail_work_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_messages", sa.Column("inbox_returned_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("email_messages", "inbox_returned_at")
