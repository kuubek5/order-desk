"""Сторож матеріалу: доступ, яким керує адмін.

Revision ID: 0104_material_guard_access
Revises: 0103_material_guard

Власник 20.11.26: адмін на кожного оператора обирає «заборонено» / «на вибір
оператора» / «завжди увімкнено». Порожній рядок = на вибір оператора — тобто
наявні акаунти поводяться рівно як до міграції.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0104_material_guard_access"
down_revision: str | None = "0103_material_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(bind, table: str) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "material_guard_access" not in _columns(op.get_bind(), "users"):
        op.add_column("users", sa.Column("material_guard_access", sa.String(10), nullable=False, server_default=""))


def downgrade() -> None:
    if "material_guard_access" in _columns(op.get_bind(), "users"):
        op.drop_column("users", "material_guard_access")
