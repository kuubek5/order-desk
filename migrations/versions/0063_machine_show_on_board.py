"""Верстат можна прибрати з табло цеху, не вимикаючи його самого.

Наявний `machines.enabled` для цього не годиться: він означає «на ремонті» й
зупиняє ОПИТУВАННЯ верстата — знімки, історію, обриви звʼязку. Власникові ж
потрібне інше: верстат працює і система за ним стежить, але на телевізорі
його місце займати не треба (прохання 15.09.26).

Усім наявним верстатам ставиться True: табло вже висить у цеху й показує їх
усі, і оновлення не має нічого з нього прибирати.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0063_machine_show_on_board"
down_revision: str | None = "0062_order_fill_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "machines",
        sa.Column("show_on_board", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("machines", "show_on_board")
