"""Сторож матеріалу: перемикачі оператора й «Погоджено».

Revision ID: 0103_material_guard
Revises: 0102_attachment_archive_name

Власник 18.11.26 (MATERIAL_GUARD_BRIEF.md): червона картка, коли замовлене
(матеріал, лінія, колір) не збігається з диском програми, що фрезерується.
Вмикається кожному оператору окремо; «лист ↔ вписане оператором» — свій
перемикач; вигляд — банер (A) чи картка-порівняння (B). «Погоджено» знімає
картку для роботи або для всього диска (невірно названий диск).
За замовчуванням усе вимкнено — наявні акаунти нічого нового не бачать.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0103_material_guard"
down_revision: str | None = "0102_attachment_archive_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    have = _columns(bind, "users")
    if "material_guard" not in have:
        op.add_column("users", sa.Column("material_guard", sa.Boolean(), nullable=False, server_default="0"))
    if "material_guard_letter" not in have:
        op.add_column("users", sa.Column("material_guard_letter", sa.Boolean(), nullable=False, server_default="0"))
    if "material_guard_view" not in have:
        op.add_column("users", sa.Column("material_guard_view", sa.String(20), nullable=False, server_default=""))
    if "material_guard_acks" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "material_guard_acks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
            sa.Column("disc_key", sa.String(160), nullable=True),
            sa.Column("layer", sa.String(10), nullable=False, server_default="disc"),
            sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_material_guard_acks_order_id", "material_guard_acks", ["order_id"])
        op.create_index("ix_material_guard_acks_disc_key", "material_guard_acks", ["disc_key"])


def downgrade() -> None:
    bind = op.get_bind()
    if "material_guard_acks" in sa.inspect(bind).get_table_names():
        op.drop_index("ix_material_guard_acks_disc_key", table_name="material_guard_acks")
        op.drop_index("ix_material_guard_acks_order_id", table_name="material_guard_acks")
        op.drop_table("material_guard_acks")
    have = _columns(bind, "users")
    for name in ("material_guard_view", "material_guard_letter", "material_guard"):
        if name in have:
            op.drop_column("users", name)
