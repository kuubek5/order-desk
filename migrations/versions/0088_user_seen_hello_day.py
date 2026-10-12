"""Ранкове привітання бачено за робочий день — на акаунті: users.seen_hello_day.

Revision ID: 0088_user_seen_hello_day
Revises: 0087_user_seen_whats_new

Привітання над чергою («Добрий день, … · N робіт сьогодні») має показатись
раз на робочий день, а памʼятало «вже бачив сьогодні» лише в localStorage
браузера — на робочих місцях цеху ця позначка не доживала до наступного
відкриття, і привітання показувалось знову й знову (власник 12.10.26, та сама
причина, що й у «Що нового», 0087). Тепер позначка — на акаунті оператора:
ISO-дата робочого дня; NULL = ще не бачив, тобто поведінка до цієї міграції.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0088_user_seen_hello_day"
down_revision: str | None = "0087_user_seen_whats_new"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "seen_hello_day"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(10), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
