"""Журнал підбору: що запропонувала картка листа і що вибрав оператор.

Revision ID: 0107_selection_journal
Revises: 0106_feedback_access

Власник 22.11.26: фіксувати, які матеріали, диски й висоти оператор бере при
прийнятті листів, щоб періодично аналізувати й будувати алгоритм підбору.
Нова таблиця, наявних даних не чіпає.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0107_selection_journal"
down_revision: str | None = "0106_feedback_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "selection_records"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("email_id", sa.Integer(), nullable=True),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(12), nullable=False, server_default="card"),
        sa.Column("batch_key", sa.String(64), nullable=True),
        sa.Column("guess", sa.String(120), nullable=True),
        sa.Column("suggested_material", sa.String(80), nullable=True),
        sa.Column("chosen_material", sa.String(80), nullable=False, server_default=""),
        sa.Column("quantity", sa.String(16), nullable=False, server_default=""),
        sa.Column("client_name", sa.String(160), nullable=True),
        sa.Column("hint_json", sa.Text(), nullable=True),
        sa.Column("hint_material", sa.String(80), nullable=True),
        sa.Column("actual_disc", sa.String(160), nullable=True),
        sa.Column("actual_height", sa.Integer(), nullable=True),
        sa.Column("actual_at", sa.DateTime(), nullable=True),
        sa.Column("actual_note", sa.String(40), nullable=True),
    )
    op.create_index("ix_selection_records_created_at", _TABLE, ["created_at"])
    op.create_index("ix_selection_records_email_id", _TABLE, ["email_id"])
    op.create_index("ix_selection_records_order_id", _TABLE, ["order_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in sa.inspect(bind).get_table_names():
        return
    for name in ("ix_selection_records_order_id", "ix_selection_records_email_id",
                 "ix_selection_records_created_at"):
        op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)
