"""Дозняти заливку поштових робіт, відмічених «знайдено» за вади 0.21.23–0.21.24.

Revision ID: 0081_refill_mail_handout_fills
Revises: 0080_user_mail_view

30.09.26, жива видача: пошта з рядком-нотаткою (`append_mail_placeholder_row`)
має рядок і синю заливку в таблиці, але `set_client_row_fill` пропускав
усе, що не «sheet_client». Галочка «знайдено» в CRM ставила статус, а
заливку в таблиці не знімала — і не лишала `fill_pending`, бо пропуск не
вважався помилкою. Тож фоновий повтор про ці роботи не знав.

Ставимо їм `fill_pending = 'clear'`: `retry_pending_fills` дознімає заливку
сам, по кілька робіт за прохід. Позиція рядка звіряється за іменем, тож чужий
рядок не зачепить; зняти заливку з уже білого рядка — нічого не змінює.
Наявну позначку (`fill_pending` вже стоїть) не перебиваємо.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0081_refill_mail_handout_fills"
down_revision: str | None = "0080_user_mail_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE orders SET fill_pending = 'clear' "
        "WHERE source = 'email' "
        "AND row_number IS NOT NULL "
        "AND sheet_tab IS NOT NULL AND sheet_tab <> '' "
        "AND archived_at IS NULL "
        "AND fill_pending IS NULL "
        "AND status IN ('знайдено при видачі', 'видано')"
    )


def downgrade() -> None:
    # Навмисно порожньо: позначка лише просить повтор фарбування, який
    # ідемпотентний; знімати її назад нема сенсу.
    pass
