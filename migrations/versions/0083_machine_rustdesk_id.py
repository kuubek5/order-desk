"""ID верстата в RustDesk: machines.rustdesk_id.

Revision ID: 0083_machine_rustdesk_id
Revises: 0082_email_reopened_at

Власник 30.09.26: клік по чіпу верстата над чергою має відкривати вікно цього
верстата в RustDesk. ID задається в Налаштуваннях → Верстати; порожній —
чіп веде на екран «Верстати», як і досі.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083_machine_rustdesk_id"
down_revision: str | None = "0082_email_reopened_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "machines"
_COLUMN = "rustdesk_id"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(40), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
