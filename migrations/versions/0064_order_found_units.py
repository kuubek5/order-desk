"""Часткова відмітка одиниць однієї роботи на видачі.

Рядок таблиці несе кілька коронок, а пічки розкладають їх по різних
закладках (CLAUDE.md §2), тож «двох знайшов, третьої ще немає» — норма
ранкової видачі. Галочка «знайдено» була все-або-нічого, і залишок
доводилось тримати в голові (прохання власника 15.09.26).

NULL для всіх наявних робіт: нічого не «частково», стан не змінюється.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0064_order_found_units"
down_revision: str | None = "0063_machine_show_on_board"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("found_units", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "found_units")
