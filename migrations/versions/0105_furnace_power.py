"""Піч без зв'язку під час спікання: записи й перемикачі.

Revision ID: 0105_furnace_power
Revises: 0104_material_guard_access

Власник 20.11.26: піч на батареї резервного живлення, батарея може сісти
посеред спікання — CRM показує повідомлення, доки його не знімуть
«Зрозуміло». Кожному оператору — власний перемикач і доступ від адміна
(заборонено / на вибір / завжди). За замовчуванням — на вибір, вимкнено.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0105_furnace_power"
down_revision: str | None = "0104_material_guard_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    have = _columns(bind, "users")
    if "furnace_power_alert" not in have:
        op.add_column("users", sa.Column("furnace_power_alert", sa.Boolean(), nullable=False, server_default="0"))
    if "furnace_power_access" not in have:
        op.add_column("users", sa.Column("furnace_power_access", sa.String(10), nullable=False, server_default=""))
    if "furnace_outages" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "furnace_outages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("furnace_key", sa.String(80), nullable=False),
            sa.Column("furnace_name", sa.String(80), nullable=False, server_default=""),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("detected_at", sa.DateTime(), nullable=False),
            sa.Column("recovered_at", sa.DateTime(), nullable=True),
            sa.Column("last_temp_c", sa.Integer(), nullable=True),
            sa.Column("acked_at", sa.DateTime(), nullable=True),
            sa.Column("acked_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.UniqueConstraint("furnace_key", "started_at", name="uq_furnace_outage_start"),
        )
        op.create_index("ix_furnace_outages_furnace_key", "furnace_outages", ["furnace_key"])
        op.create_index("ix_furnace_outages_acked_at", "furnace_outages", ["acked_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if "furnace_outages" in sa.inspect(bind).get_table_names():
        op.drop_index("ix_furnace_outages_acked_at", table_name="furnace_outages")
        op.drop_index("ix_furnace_outages_furnace_key", table_name="furnace_outages")
        op.drop_table("furnace_outages")
    have = _columns(bind, "users")
    for name in ("furnace_power_access", "furnace_power_alert"):
        if name in have:
            op.drop_column("users", name)
