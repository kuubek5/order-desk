"""Візуальний набір оператора: кнопки, індикатор очікування, чіпи.

Revision ID: 0028_add_user_ui_elements
Revises: 0027_add_user_ui_prefs

Продовження 0027: там були тема й іконки, тут решта елементів із галереї
«графічний фонд» — щоб оператор міг зібрати вигляд під себе, а не вибирати
з двох параметрів.

Порожній рядок скрізь = канон, тому наявні користувачі не помічають нічого.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0028_add_user_ui_elements"
down_revision: str | None = "0027_add_user_ui_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("ui_button_style", "ui_loader_style", "ui_chip_style")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "users",
            sa.Column(name, sa.String(length=20), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("users", name)
