"""Тека програм фрезерування верстата: machines.program_folder.

Revision ID: 0099_machine_program_folder
Revises: 0098_client_fact_decisions

Власник 12.11.26: Sum3D кладе `.iso` або в розшарену теку на ПК з CRM (звідти
старі верстати забирають програму скриптом), або одразу в розшарену теку
нового верстата. Шлях задається в Налаштуваннях → Верстати; CRM цю теку лише
ЧИТАЄ (звірка диска програми з замовленням). Порожньо — теки не задано.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0099_machine_program_folder"
down_revision: str | None = "0098_client_fact_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "machines"
_COLUMN = "program_folder"


def _has_column(bind) -> bool:
    return _COLUMN in {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(400), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
