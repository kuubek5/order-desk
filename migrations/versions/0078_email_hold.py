"""Лист «На уточненні»: пауза в CRM (власник 25.09.26).

Revision ID: 0078_email_hold
Revises: 0077_email_inbox_returned

Клієнт надіслав дубль, не надіслав файлів чи не вказав матеріал — лист ставлять
на паузу, адміністратори уточнюють у замовника телефоном. Лише стан CRM
(скринька не змінюється): `hold_at` виводить лист із «Вхідних» у вкладку
«На уточненні», причина — `hold_reason` (+ `hold_note` для «інше»), хто —
`hold_by`. Усі nullable, NULL для наявних листів — поведінка не змінюється.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0078_email_hold"
down_revision: str | None = "0077_email_inbox_returned"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("hold_at", sa.DateTime(), nullable=True))
    op.add_column("email_messages", sa.Column("hold_reason", sa.String(length=40), nullable=True))
    op.add_column("email_messages", sa.Column("hold_note", sa.String(length=300), nullable=True))
    op.add_column("email_messages", sa.Column("hold_by", sa.String(length=100), nullable=True))
    op.create_index("ix_email_messages_hold_at", "email_messages", ["hold_at"])


def downgrade() -> None:
    op.drop_index("ix_email_messages_hold_at", table_name="email_messages")
    with op.batch_alter_table("email_messages") as batch:
        batch.drop_column("hold_by")
        batch.drop_column("hold_note")
        batch.drop_column("hold_reason")
        batch.drop_column("hold_at")
