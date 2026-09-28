"""Вигляд екрана пошти на акаунті: users.mail_view.

Revision ID: 0080_user_mail_view
Revises: 0079_clear_untaken_sheet_change_flags

Класичний екран доведений (V1, MAIL_V1_BRIEF.md, 28.09.26); поверх нього —
два ОПЦІЙНІ вигляди, оператор перемикає під себе. "" — класика (нинішня
поведінка), "conversation" — «Лист як розмова» (A), "focus" — «Лист ↔
заявка» (C). Живе на акаунті, як теми й шестерня: вигляд їде за оператором,
а не за браузером цього ПК.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080_user_mail_view"
down_revision: str | None = "0079_clear_untaken_sheet_change_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "mail_view"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(20), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
