"""Зведені роботи з пошти: order_emails.

Revision ID: 0084_order_emails
Revises: 0083_machine_rustdesk_id

Власник 05.10.26: у Конвеєрі кілька листів одного клієнта з одним кольором, що
лягли в одну теку, стають ОДНІЄЮ роботою й одним рядком у таблиці з сумою
кількості. Таблиця тримає внесок кожного листа, щоб відкат одного листа
віднімав лише його кількість. Вмикається перемикачем у Налаштуваннях →
Ранкова видача; поки він вимкнений, таблиця лишається порожньою.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084_order_emails"
down_revision: str | None = "0083_machine_rustdesk_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "order_emails"


def _has_table(bind) -> bool:
    return _TABLE in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column(
            "email_message_id", sa.Integer(),
            sa.ForeignKey("email_messages.id"), nullable=False,
        ),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opak_units", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_emails_order_id", _TABLE, ["order_id"])
    op.create_index("ix_order_emails_email_message_id", _TABLE, ["email_message_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        op.drop_index("ix_order_emails_email_message_id", table_name=_TABLE)
        op.drop_index("ix_order_emails_order_id", table_name=_TABLE)
        op.drop_table(_TABLE)
