"""Верстати: таблиця machines — дзеркало furnaces.

Revision ID: 0032_add_machines
Revises: 0031_add_user_handout_layout

Фрезерні верстати підключаються так само, як печі: view-only VNC на ПК
верстата, CRM читає екран. Таблиця повторює форму furnaces свідомо — обидві
описують «залізо з екраном за VNC», і однакова форма дає спільні патерни
сервісу. Нічого не переносимо: верстатів у налаштуваннях раніше не було.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0032_add_machines"
down_revision: str | None = "0031_add_user_handout_layout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "machines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("host", sa.String(length=60), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="5900"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("password_encrypted", sa.String(length=500), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("host", "port", name="uq_machine_host_port"),
    )


def downgrade() -> None:
    op.drop_table("machines")
