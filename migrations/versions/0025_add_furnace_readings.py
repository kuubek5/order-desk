"""Показання табло печей спікання, зняті по VNC.

Revision ID: 0025_add_furnace_readings
Revises: 0024_add_order_focus

Піч не віддає даних жодним чистим протоколом (FTP під невідомим паролем,
Modbus/OPC вимкнені), тож єдиний канал — її власний екран. Тут лежить те, що
з нього прочиталось: статус, температура, залишок часу.

Рядок пишеться на ЗМІНУ, а не на кожен кадр — кадр знімається кожні кілька
секунд, і без цього правила одна піч давала б ~17 тис. рядків на добу.

Порожні temp_c / remaining_seconds — нормальний стан: невпізнане число не
здогадується, а лишається порожнім, і поруч зберігається сире прочитання
(raw_*) зі знаками «?» на місцях, які не збіглись з еталоном.

Див. app/models.py::FurnaceReading, app/furnace_ocr.py.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0025_add_furnace_readings"
down_revision: str | None = "0024_add_order_focus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "furnace_readings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("host", sa.String(length=60), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("temp_c", sa.Integer(), nullable=True),
        sa.Column("remaining_seconds", sa.Integer(), nullable=True),
        sa.Column("elapsed_seconds", sa.Integer(), nullable=True),
        sa.Column("command", sa.String(length=40), nullable=True),
        sa.Column("raw_temp", sa.String(length=20), nullable=True),
        sa.Column("raw_remaining", sa.String(length=20), nullable=True),
        sa.Column("error", sa.String(length=300), nullable=True),
    )
    op.create_index("ix_furnace_readings_host", "furnace_readings", ["host"])
    op.create_index("ix_furnace_readings_captured_at", "furnace_readings", ["captured_at"])


def downgrade() -> None:
    op.drop_index("ix_furnace_readings_captured_at", table_name="furnace_readings")
    op.drop_index("ix_furnace_readings_host", table_name="furnace_readings")
    op.drop_table("furnace_readings")
