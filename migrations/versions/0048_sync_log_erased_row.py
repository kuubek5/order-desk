"""Слід стертого рядка в структурованому вигляді: sync_logs.erased_*.

Revision ID: 0048_sync_log_erased_row
Revises: 0047_vyrobitok_day_freeze

Вміст стертого рядка вже потрапляв у журнал — але всередині тексту
`message`, звідки його читає лише людина. Щоб дати кнопку «Відновити рядок»,
той самий вміст треба зберігати як дані:

* `erased_row` — номер рядка у вкладці, який стерли;
* `erased_values` — JSON-масив значень A:K до стирання;
* `erased_restored_at` — коли рядок повернули (кнопка зникає, повторне
  відновлення неможливе).

Тільки схема, наявні записи лишаються як є: у старих рядків журналу цих полів
немає, тож кнопки в них не буде — вміст там і далі читається з тексту.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_sync_log_erased_row"
down_revision: str | None = "0047_vyrobitok_day_freeze"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sync_logs"
_COLUMNS = {
    "erased_row": sa.Column("erased_row", sa.Integer(), nullable=True),
    "erased_values": sa.Column("erased_values", sa.Text(), nullable=True),
    "erased_restored_at": sa.Column("erased_restored_at", sa.DateTime(), nullable=True),
}


def _existing(bind) -> set[str]:
    return {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)
    for name, column in _COLUMNS.items():
        if name not in present:
            op.add_column(_TABLE, column)


def downgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)
    for name in _COLUMNS:
        if name in present:
            op.drop_column(_TABLE, name)
