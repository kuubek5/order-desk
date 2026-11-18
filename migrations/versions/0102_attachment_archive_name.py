"""Назва архіву, з якого розпаковано вкладення: attachments.archive_name.

Revision ID: 0102_attachment_archive_name
Revises: 0101_machine_agent_version

Власник 18.11.26: коли в листі не сказано матеріал і колір, клієнт буває пише
їх у назві архіву («Іванов zr A2.zip»). Після розпакування рядок архіву
видалявся, і назва губилась назавжди. Порожньо для всього, що розпаковано
раніше, і для вкладень не з архіву.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0102_attachment_archive_name"
down_revision: str | None = "0101_machine_agent_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "attachments"


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    if "archive_name" not in _existing(op.get_bind()):
        op.add_column(_TABLE, sa.Column("archive_name", sa.String(300), nullable=True))


def downgrade() -> None:
    if "archive_name" in _existing(op.get_bind()):
        op.drop_column(_TABLE, "archive_name")
