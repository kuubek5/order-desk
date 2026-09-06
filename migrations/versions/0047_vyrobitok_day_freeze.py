"""Заморозка дня у «Виробітку»: таблиця vyrobitok_days.

Revision ID: 0047_vyrobitok_day_freeze
Revises: 0046_saved_queue_views

День табеля закривається за розкладом: лабораторія й пошта — о 07:30 (кінець
робочої доби), СЛМ — о 18:00 того ж дня (його дописують і правлять ще пів дня
після кінця доби). Після заморозки число читається лише зі знімка, і ні чистка
старої вкладки, ні повний імпорт історії його не зачіпають.

Таблиця нова, тож на чистій базі її зробить `create_all`; ця міграція потрібна
наявним інсталяціям. Ідемпотентна.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0047_vyrobitok_day_freeze"
down_revision: str | None = "0046_saved_queue_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "vyrobitok_days"


def _has_table(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("orders_frozen_at", sa.DateTime(), nullable=True),
        sa.Column("slm_frozen_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        op.drop_table(_TABLE)
