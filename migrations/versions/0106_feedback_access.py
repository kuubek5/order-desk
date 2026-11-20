"""Кнопка «Написати нам»: власний перемикач і доступ від адміна.

Revision ID: 0106_feedback_access
Revises: 0105_furnace_power

Власник 20.11.26: функцію зворотного зв'язку треба мати змогу вимкнути.
Перемикач за замовчуванням УВІМКНЕНИЙ, доступ — «на вибір оператора»: наявні
акаунти бачать кнопку рівно як до міграції.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0106_feedback_access"
down_revision: str | None = "0105_furnace_power"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    have = _columns(op.get_bind(), "users")
    if "feedback_beacon" not in have:
        op.add_column("users", sa.Column("feedback_beacon", sa.Boolean(), nullable=False, server_default="1"))
    if "feedback_access" not in have:
        op.add_column("users", sa.Column("feedback_access", sa.String(10), nullable=False, server_default=""))


def downgrade() -> None:
    have = _columns(op.get_bind(), "users")
    for name in ("feedback_access", "feedback_beacon"):
        if name in have:
            op.drop_column("users", name)
