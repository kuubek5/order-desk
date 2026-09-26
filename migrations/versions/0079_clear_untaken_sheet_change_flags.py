"""Зняти позначку «технік змінив» з робіт, яких ще не взяли (без Sum3D).

Revision ID: 0079_clear_untaken_sheet_change_flags
Revises: 0078_email_hold

Власник 26.09.26: позначка висіла постійно, а потрібна лише після того, як
оператор узяв роботу (ввів Sum3D) — до цього правка техніка є звичайним
доопрацюванням. Синк тепер ставить її лише на взяту роботу й знімає з
невзятої (app/sync.py, `taken_before_pass`), але фоновий синк перечитує
тільки сьогодні±1: позначки на старіших вкладках, поставлені за старим
правилом, лишились би назавжди — і в черзі, і в лічильнику спливаючого
сповіщення, а після введення Sum3D показали б «змінено після взяття» про
зміну, що сталась ДО взяття.

Історію картки («технік змінив у таблиці: …») не чіпаємо — це аудит.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0079_clear_untaken_sheet_change_flags"
down_revision: str | None = "0078_email_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE orders SET sheet_changed_at = NULL, sheet_changed_fields = NULL "
        "WHERE sheet_changed_at IS NOT NULL "
        "AND (sum3d_id IS NULL OR TRIM(sum3d_id) = '')"
    )


def downgrade() -> None:
    # Навмисно порожньо: зняті позначки описували зміни до взяття, яких
    # оператору показувати не треба.
    pass
