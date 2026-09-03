"""Віджет верстатів: фоновий арт і вигляд стрічки — на акаунті оператора.

Revision ID: 0036_add_user_machine_widget_prefs
Revises: 0035_machine_agent_token

Галерея 03.09.26 дала п'ять фонів секції «Верстати» і п'ять стрічок «назва +
відсоток» над чергою. Власник обрав «Пил на сталі» і «Сегменти» дефолтом,
решту лишив як вибір оператора. Порожній рядок = дефолт, тому наявні
акаунти бачать рівно те, що обрано.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0036_add_user_machine_widget_prefs"
down_revision: str | None = "0035_machine_agent_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("ui_machine_art", "ui_machine_strip")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "users",
            sa.Column(name, sa.String(length=20), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("users", name)
