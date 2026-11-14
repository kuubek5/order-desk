"""Памʼять акаунта: users.mail_fam (фільтр матеріалу пошти) і users.ui_store.

Revision ID: 0100_user_account_memory
Revises: 0099_machine_program_folder

Власник 14.11.26: фільтр «Цирконій» у пошті збивався після прийняття листа, а
налаштування вигляду жили лише в браузері й губились на іншому ПК. Обидві
колонки порожні за замовчуванням — наявні акаунти бачать усе, як і раніше.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0100_user_account_memory"
down_revision: str | None = "0099_machine_program_folder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    have = _existing(op.get_bind())
    if "mail_fam" not in have:
        op.add_column(_TABLE, sa.Column("mail_fam", sa.String(120), nullable=False, server_default=""))
    if "ui_store" not in have:
        op.add_column(_TABLE, sa.Column("ui_store", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    have = _existing(op.get_bind())
    for name in ("mail_fam", "ui_store"):
        if name in have:
            op.drop_column(_TABLE, name)
