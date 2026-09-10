"""Які віджети черги сховано — на акаунті.

Revision ID: 0056_user_queue_hidden_widgets
Revises: 0055_machine_link_events

Смуга верстатів і віджет Sisma в шапці черги, а також секції бокової панелі
тепер вимикаються в шестерні вигляду. CSV схованих ключів (machines, sisma,
side-mail, side-furnace…); порожньо = усе видно, тому наявні акаунти бачать
те саме, що й раніше.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0056_user_queue_hidden_widgets"
down_revision: str | None = "0055_machine_link_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("queue_hidden_widgets", sa.String(length=120),
                  nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("users", "queue_hidden_widgets")
