"""Прибрати мертву колонку attachments.staged_to_export.

Revision ID: 0049_drop_staged_to_export
Revises: 0048_sync_log_erased_row

Прапорець мав означати «файл уже викладено в export автоматично, при
прийнятті його не рухати вдруге». Його ніхто й ніколи не вмикав: у коді не
було жодного присвоєння True — лише скидання в False і три гілки, які через
це не виконувались ніколи. Мертвий прапорець гірший за відсутній: він
описує механізм, якого немає, і наступний читач планує навколо нього.

Дані не втрачаються: колонка всюди False.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_drop_staged_to_export"
down_revision: str | None = "0048_sync_log_erased_row"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "attachments"
_COLUMN = "staged_to_export"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
