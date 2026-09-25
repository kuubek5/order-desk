"""Повернути поштовим роботам `source = "email"`, яке з'їв синк.

Revision ID: 0076_restore_mail_work_source
Revises: 0075_email_mailbox_moved_at

Прийняття листа пише в таблицю рядок-нотатку тієї ж форми, що наряд-less
клієнтський, і синк, читаючи його, вважав, що «в рядку інша робота»
(`existing.source != source`), та скидав поштову роботу в "sheet_client"
(виправлено в app/sync.py, `own_mail_note`, 25.09.26). Наслідки: робота
зникала з «Прийняте з пошти», видача втрачала точну теку (`export_folder_path`
береться лише для email).

Ознака однозначна: `source_email_id` ставить ЛИШЕ прийняття листа, а скидання
(`_reset_order_for_new_work`) його не чіпає. Робота "sheet_client" з
`source_email_id` — це поштова робота, зіпсована саме цим. Інших шляхів туди
немає: робота з іншою ідентичністю в тому ж рядку синк створює окремо, а не
перевтілює (перевірено тестами 25.09.26).

Вид роботи (`kind`) скидання стерло безповоротно — відновлювати нема з чого.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0076_restore_mail_work_source"
down_revision: str | None = "0075_email_mailbox_moved_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE orders SET source = 'email' "
        "WHERE source = 'sheet_client' AND source_email_id IS NOT NULL"
    )


def downgrade() -> None:
    # Навмисно порожньо: повертати зіпсований стан немає сенсу, а відрізнити
    # виправлені рядки від споконвічно поштових уже неможливо.
    pass
