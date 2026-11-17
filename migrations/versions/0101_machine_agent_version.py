"""Версія агента на верстаті: machines.agent_version, machines.agent_version_at.

Revision ID: 0101_machine_agent_version
Revises: 0100_user_account_memory

17.11.26: агент віддає версію в `GET /status`, але CRM її не зберігала — який
білд стоїть на якому верстаті, було видно лише на самому ПК. Колонки заповнює
воркер `kuubmill-agent-versions` (`app/services/agent_versions.py`).
"" = ще не питали або агент не відповів жодного разу.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0101_machine_agent_version"
down_revision: str | None = "0100_user_account_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "machines"


def _columns(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    have = _columns(bind)
    if "agent_version" not in have:
        op.add_column(_TABLE, sa.Column("agent_version", sa.String(60), nullable=False, server_default=""))
    if "agent_version_at" not in have:
        op.add_column(_TABLE, sa.Column("agent_version_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    have = _columns(bind)
    if "agent_version_at" in have:
        op.drop_column(_TABLE, "agent_version_at")
    if "agent_version" in have:
        op.drop_column(_TABLE, "agent_version")
