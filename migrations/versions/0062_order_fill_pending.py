"""Заливка рядка, яка не доїхала в таблицю, більше не губиться мовчки.

Revision ID: 0062_order_fill_pending
Revises: 0061_screen_puzzles

Оператор ставить на видачі галочку «знайдено» — у CRM робота стає виданою, а
портал знімає синю заливку рядка в Google Таблиці. Саме знята заливка, а не
статус у базі, є сигналом видачі для всієї лабораторії (CLAUDE.md §2).

Доти невдалий запис цієї заливки був НІМИЙ: лише рядок у лог-файлі, жодного
тоста, жодного запису в журнал і жодного повтору. Обрив мережі на пів хвилини
означав, що в таблиці назавжди лишалось синє — тобто «не видано» для логістів,
адміністраторів і техніків, тоді як оператор був упевнений, що видав.

Колонка тримає заливку, яку портал збирався поставити, але не зміг: "blue" або
"clear". Поки вона непорожня, фоновий повтор домальовує її в таблицю, а рядок
видачі показує розбіжність. Усім наявним рядкам ставиться NULL — «таблиця й
портал згодні», що для вже виданих робіт правда: їхня заливка або доїхала, або
загублена безповоротно ще до цієї версії, і вигадувати за неї стан не можна.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0062_order_fill_pending"
down_revision: str | None = "0061_screen_puzzles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("fill_pending", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "fill_pending")
