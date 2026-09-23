"""Показне ім'я відправника листа.

Revision ID: 0070_email_from_name
Revises: 0069_whats_wrong_mutes

Заголовок From несе показне ім'я ("Юрій Струбицький") окремо від адреси.
imap-tools дає його як from_values.name; ми його викидали. Колонка тримає це
ім'я, щоб список тріажу показував людину, а не адресу, коли класифікатор не
вгадав клієнта. NULL для всіх наявних листів — беккфіл не потрібен: нові листи
заповнюються самі при наступному синку, а для старих список падає на здогад/
адресу, як і досі.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0070_email_from_name"
down_revision: str | None = "0069_whats_wrong_mutes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("email_messages", sa.Column("from_name", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("email_messages", "from_name")
