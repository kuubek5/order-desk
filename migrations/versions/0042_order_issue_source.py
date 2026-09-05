"""Звідки взялось «видано» — з CRM чи зі зниклої заливки в таблиці.

Revision ID: 0042_order_issue_source
Revises: 0041_user_queue_load_metrics

Бойовий випадок 01.09.26: адміністратор видалив рядки між блоком лабораторії
та блоком файлів, дані з'їхали вгору, а заливка лишилась на старих клітинках.
Синк читає заливку ЗА НОМЕРОМ РЯДКА, тому два клієнтські рядки прочитались як
«немає заливки» = видано, і зникли з ранкової видачі. Ніхто про це не дізнався
б, поки клієнт не подзвонив.

Відновити зв'язок «заливка ↔ робота» неможливо: колір належить клітинці, дані
рядку, рядок зсувається. Тому мета не «не помилятись», а НЕ ХОВАТИ помилку.

`issued_source` розрізняє два «видано», які досі виглядали однаково:
  * "portal" — оператор натиснув «Видати», є запис в історії з іменем. Факт.
  * "sheet"  — виведено зі зниклої заливки. Здогад, і саме він буває хибним.

`issue_locked` — рішення людини б'є ознаку кольору назавжди для цієї роботи.
Без нього зняття галочки не пережило б наступний синк: у таблиці заливки як
не було, так і немає, і синк знову поставив би «видано».
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0042_order_issue_source"
down_revision: str | None = "0041_user_queue_load_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("issued_source", sa.String(length=10), nullable=True))
    op.add_column(
        "orders",
        sa.Column("issue_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Наявні «видано» позначаємо як portal: до цієї міграції видача через CRM
    # була єдиним ШТАТНИМ шляхом, а помилкові — рідкісний виняток. Позначити їх
    # усі як "sheet" означало б засипати екран знаками питання в перший же день
    # і привчити оператора їх не читати.
    op.execute("UPDATE orders SET issued_source = 'portal' WHERE status = 'видано'")


def downgrade() -> None:
    op.drop_column("orders", "issue_locked")
    op.drop_column("orders", "issued_source")
