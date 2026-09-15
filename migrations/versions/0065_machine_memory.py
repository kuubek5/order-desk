"""Памʼять про верстат, що переживає перезапуск застосунку.

Стан верстата живе в памʼяті процесу, і два значення з нього після рестарту
коштували цеху хвилини неправди на телевізорі: відлік «скільки смуга стоїть на
сотні» починався заново (всі завершені верстати дві хвилини показували
«фрезерує 100 %»), а остання бачена програма зникала зовсім — і завершений
верстат лишався без назви роботи.

Таблиця маленька: рядок на верстат, ключ — адреса. Дивись `MachineMemory` і
`machines.MEMORY_MAX_GAP_SECONDS`.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0065_machine_memory"
down_revision: str | None = "0064_order_found_units"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "machine_memory",
        sa.Column("key", sa.String(length=80), primary_key=True),
        sa.Column("percent", sa.Integer(), nullable=True),
        sa.Column("percent_changed_at", sa.DateTime(), nullable=True),
        sa.Column("last_sum3d_id", sa.String(length=40), nullable=True),
        sa.Column("last_program_at", sa.DateTime(), nullable=True),
        sa.Column("saved_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("machine_memory")
