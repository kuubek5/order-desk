"""Покоління сесій користувача: users.session_epoch.

Revision ID: 0050_user_session_epoch
Revises: 0049_drop_staged_to_export

Зміна пароля не обривала вже видані сесії: вкрадена або просто забута на
чужому екрані сесія переживала саме ту дію, якою її й намагались обірвати.
Лічильник збільшується при зміні пароля, і кожна сесія зі старим значенням
перестає діяти.

Наявні користувачі дістають 0 — їхні поточні сесії лишаються дійсними, поки
пароль не змінять.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_user_session_epoch"
down_revision: str | None = "0049_drop_staged_to_export"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "session_epoch"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
