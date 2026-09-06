"""Збережені вигляди черги: таблиця saved_queue_views.

Revision ID: 0046_saved_queue_views
Revises: 0045_add_email_uid_validity

Оператор щоранку ставить той самий набір чіпів черги. Тепер набір
зберігається під назвою НА АКАУНТ (а не в localStorage браузера) і
повертається одним кліком.

Таблиця нова, тож на чистій базі її зробить `create_all`; ця міграція потрібна
наявним інсталяціям. Ідемпотентна: якщо таблиця вже є (база народилась через
`create_all` після оновлення коду), крок пропускається — інакше `alembic
upgrade head` упав би на «table already exists».
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0046_saved_queue_views"
down_revision: str | None = "0045_add_email_uid_validity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "saved_queue_views"


def _has_table(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        # Рядок GET-параметрів черги — той самий формат, що в адресі екрана.
        sa.Column("query", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        # Локальний час: server_default=func.now() на SQLite пише UTC.
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "name", name="uq_saved_queue_view_user_name"),
    )
    op.create_index("ix_saved_queue_views_user_id", _TABLE, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind):
        return
    op.drop_index("ix_saved_queue_views_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
