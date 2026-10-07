"""Док черги на екрані пошти: users.mail_dock_scope / mail_dock_ready /
mail_dock_height.

Revision ID: 0086_user_mail_dock
Revises: 0085_order_sheet_row_pending

MAIL_LAB_DOCK_BRIEF.md (07.10.26, варіант A «нижній док»): дзеркало «Прийняте
з пошти» внизу екрана пошти отримує перемикач джерела ("" — лише пошта, як
було; "lab" — плюс лабораторія; "all" — уся вкладка дня), фільтр готовності
для лабораторних і табличних рядків ("" = «можна брати») і висоту доку в px
(0 = як було). Усе на акаунті, як решта шестерні: вигляд їде за оператором.
Дефолти = нинішня поведінка, тож наявні акаунти нічого не помічають.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0086_user_mail_dock"
down_revision: str | None = "0085_order_sheet_row_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMNS = (
    sa.Column("mail_dock_scope", sa.String(20), nullable=False, server_default=""),
    sa.Column("mail_dock_ready", sa.String(20), nullable=False, server_default=""),
    sa.Column("mail_dock_height", sa.Integer(), nullable=False, server_default="0"),
)


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)
    for column in _COLUMNS:
        if column.name not in present:
            op.add_column(_TABLE, column)


def downgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)
    for column in _COLUMNS:
        if column.name in present:
            op.drop_column(_TABLE, column.name)
