"""Прийнятий лист, повернутий у Вхідні прямо в пошті: email_messages.reopened_at.

Revision ID: 0082_email_reopened_at
Revises: 0081_refill_mail_handout_fills

Власник 30.09.26: лист, уже прийнятий у CRM, у пошті повертають зі «Скачено,
просчитано» у Вхідні (помилково перенесли, переробка, дописати файли). Синк
знімав мітку папки, але статус «прийнято» лишав лист в «Архіві» — у «Вхідних»
CRM його не було. Позначка показує такий лист у «Вхідних»; робота не чіпається.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082_email_reopened_at"
down_revision: str | None = "0081_refill_mail_handout_fills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "email_messages"
_COLUMN = "reopened_at"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.DateTime(timezone=False), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
