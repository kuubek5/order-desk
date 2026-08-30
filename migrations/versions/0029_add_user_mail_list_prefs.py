"""Вигляд списку листів на акаунті оператора: відступ рядка, ширина, крок.

Revision ID: 0029_add_user_mail_list_prefs
Revises: 0028_add_user_ui_elements

Продовження лінії 0027/0028: візуальне налаштування живе на користувачі, а не
в localStorage, тому їде за оператором і повертається при наступному вході.

Канон — 0 у всіх трьох: рівно те, що бачить оператор, який нічого не крутив.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0029_add_user_mail_list_prefs"
down_revision: str | None = "0028_add_user_ui_elements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("mail_row_pad", "mail_list_width", "mail_ui_step")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "users",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("users", name)
