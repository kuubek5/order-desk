"""UIDVALIDITY у дедупі листів: колонка email_messages.uid_validity.

Revision ID: 0045_add_email_uid_validity
Revises: 0044_order_status_indexes

IMAP UID унікальний лише в межах поточного UIDVALIDITY теки. Досі дедуп ішов
самим `uid` (унікальний індекс `ix_email_messages_uid`): якби ukr.net коли-небудь
перестворив скриньку, нумерація почалася б спочатку, і старий рядок з тим самим
uid видав би НОВИЙ лист за «вже імпортований» — лист зник би мовчки, а це саме
те, чого CLAUDE.md (екран 2) не дозволяє.

Міграція:
* додає `uid_validity` (NOT NULL, дефолт '' — «namespace невідомий»; перший
  синк після оновлення проставляє наявним рядкам поточне значення);
* знімає УНІКАЛЬНІСТЬ з індексу по `uid` (лишає звичайний, за ним і далі
  шукають рядок при повторному фетчі);
* створює унікальний складений `ix_email_messages_uid_validity` (uid,
  uid_validity) — унікальність усередині namespace збережена, а той самий uid
  з іншого namespace більше не конфліктує.

Дані не змінюються, лише схема. Ім'я старого індексу могло відрізнятись
(бази народжувались і через `create_all`), тому шукаємо його в PRAGMA, а не
покладаємось на константу.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0045_add_email_uid_validity"
down_revision: str | None = "0044_order_status_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "email_messages"
_COMPOSITE = "ix_email_messages_uid_validity"


def _uid_indexes(bind) -> list[tuple[str, int]]:
    """(ім'я, чи унікальний) для індексів, побудованих РІВНО по колонці uid."""
    found = []
    for row in bind.execute(sa.text(f"PRAGMA index_list({_TABLE})")).fetchall():
        name, unique = row[1], row[2]
        if name == _COMPOSITE:
            continue
        columns = [c[2] for c in bind.execute(sa.text(f"PRAGMA index_info({name})")).fetchall()]
        if columns == ["uid"]:
            found.append((name, int(unique)))
    return found


def _has_column(bind, column: str) -> bool:
    return any(
        row[1] == column
        for row in bind.execute(sa.text(f"PRAGMA table_info({_TABLE})")).fetchall()
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "uid_validity"):
        op.add_column(
            _TABLE,
            sa.Column("uid_validity", sa.String(50), nullable=False, server_default=""),
        )
    for name, unique in _uid_indexes(bind):
        if unique:
            # Унікальний індекс по одному uid — саме він і блокував би другий
            # рядок з тим самим uid у новому namespace.
            op.drop_index(name, table_name=_TABLE)
    if not _uid_indexes(bind):
        op.create_index("ix_email_messages_uid", _TABLE, ["uid"])
    existing = {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({_TABLE})")).fetchall()}
    if _COMPOSITE not in existing:
        op.create_index(_COMPOSITE, _TABLE, ["uid", "uid_validity"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    existing = {row[1] for row in bind.execute(sa.text(f"PRAGMA index_list({_TABLE})")).fetchall()}
    if _COMPOSITE in existing:
        op.drop_index(_COMPOSITE, table_name=_TABLE)
    for name, unique in _uid_indexes(bind):
        if not unique:
            op.drop_index(name, table_name=_TABLE)
    op.create_index("ix_email_messages_uid", _TABLE, ["uid"], unique=True)
    if _has_column(bind, "uid_validity"):
        op.drop_column(_TABLE, "uid_validity")
