"""«Що нового» бачено до версії — на акаунті: users.seen_whats_new.

Revision ID: 0087_user_seen_whats_new
Revises: 0086_user_mail_dock

Картка «Що нового» після оновлення памʼятала «вже бачив» лише в localStorage
браузера. На робочих місцях цеху ця позначка не доживала до наступного
відкриття (дані сайту чистяться при закритті браузера, другий ПК, інша адреса
того самого сервера), і картка показувалась операторам знову й знову (власник
12.10.26). Тепер позначка — на акаунті оператора; NULL = ще не бачив жодної,
тобто рівно та поведінка, що й до цієї міграції.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0087_user_seen_whats_new"
down_revision: str | None = "0086_user_mail_dock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "seen_whats_new"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(20), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
