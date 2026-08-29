"""Робочий набір оператора: особиста мітка «беру зараз» на роботі.

Revision ID: 0024_add_order_focus
Revises: 0023_add_shift_notes

Взявши кілька нарядів, оператор має не загубити, куди вписувати Sum3D ID —
на папері це робиться маркером. Мітка персональна (оператор у ключі), бо
одну роботу можуть тримати в наборі двоє, і чужа мітка не сміє затирати мою.

Унікальна пара (order_id, user_id) — не косметика: без неї подвійний клік
дає два рядки, і лічильник у фільтрі починає брехати.

Див. app/models.py::OrderFocus.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0024_add_order_focus"
down_revision: str | None = "0023_add_shift_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_focus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("order_id", "user_id", name="uq_order_focus_order_user"),
    )
    op.create_index("ix_order_focus_order_id", "order_focus", ["order_id"])
    op.create_index("ix_order_focus_user_id", "order_focus", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_order_focus_user_id", table_name="order_focus")
    op.drop_index("ix_order_focus_order_id", table_name="order_focus")
    op.drop_table("order_focus")
