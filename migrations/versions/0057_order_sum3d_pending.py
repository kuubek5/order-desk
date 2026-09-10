"""Sum3D ID, що ще не дійшов у таблицю.

Revision ID: 0057_order_sum3d_pending
Revises: 0056_user_queue_hidden_widgets

Оператор вписує Sum3D ID у черзі; база зберігає його одразу, таблиця — після.
Якщо запис у таблицю не підтвердився, наступний синк бачив порожню колонку L
і стирав ID у базі (таблиця головна для Sum3D). Колонка тримає значення, яке
чекає на запис: поки воно тут, порожня L його не стирає, а фоновий повтор
дописує. NULL для всіх наявних робіт — розбіжностей на момент міграції не
відомо, поведінка для них та сама, що й досі.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0057_order_sum3d_pending"
down_revision: str | None = "0056_user_queue_hidden_widgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("sum3d_pending", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "sum3d_pending")
