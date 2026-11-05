"""Док пошти без «+ Лабораторія» і «Не готово»: перевести збережені вибори.

Revision ID: 0096_mail_dock_retired_filters
Revises: 0095_furnace_mac

Власник 05.11.26: у доці забагато фільтрів, ці дві кнопки прибрано. Оператор, у
якого вибір уже збережено, лишився б із фільтром без підсвіченої кнопки, тож
переводимо на те, що нічого не ховає: «Уся вкладка» (там і пошта, і лаба) та «Усі»
(там і неготові рядки). Схема не змінюється, відкочувати нічого: старий вибір
після відкату просто лишиться новим.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0096_mail_dock_retired_filters"
down_revision: str | None = "0095_furnace_mac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE users SET mail_dock_scope = 'all' WHERE mail_dock_scope = 'lab'")
    op.execute("UPDATE users SET mail_dock_ready = 'all' WHERE mail_dock_ready = 'not_ready'")


def downgrade() -> None:
    pass
