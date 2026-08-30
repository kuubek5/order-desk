"""Візуальні налаштування оператора: тема і стиль іконок на акаунті.

Revision ID: 0027_add_user_ui_prefs
Revises: 0026_add_furnaces

Тема «Amber Forge» спершу жила в localStorage — тобто в браузері, а не в
оператора: інший профіль чи перевстановлення губили вибір, і всі оператори
одного ПК ділили одну тему. Тепер вибір — поля акаунта, рендеряться
сервер-сайд у base.html атрибутами на <html>.

Порожній рядок = канон (бірюзовий пульт / контурні іконки), тому наявні
користувачі нічого не помічають.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0027_add_user_ui_prefs"
down_revision: str | None = "0026_add_furnaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ui_theme", sa.String(length=20), nullable=False, server_default=""),
    )
    op.add_column(
        "users",
        sa.Column("ui_icon_style", sa.String(length=20), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("users", "ui_icon_style")
    op.drop_column("users", "ui_theme")
