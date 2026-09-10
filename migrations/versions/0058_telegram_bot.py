"""Двосторонній Telegram-бот: черга сповіщень і пам'ять переходів.

Revision ID: 0058_telegram_bot
Revises: 0057_order_sum3d_pending

Рома бачить стан цеху з телефона (бриф TELEGRAM_BOT_BRIEF.md, 10.09.26).
Меню відповідає на запит і нічого не зберігає; дві таблиці тут — для
сповіщень, які бот пише сам.

`telegram_outbox` — черга відправки. Подія спершу стає рядком, фоновий
відправник доносить його з ретраєм. Унікальний `dedup_key` не дає рестарту
послати те саме «пічка закрилась» двічі.

`telegram_watch` — останній підтверджений стан кожної печі й принтера. Без
нього перший кадр після рестарту читався б як перехід, і кожен запуск
застосунку сипав би сповіщеннями про цикли, що йдуть уже годину.

Час локальний і без серверного дефолту — як у показаннях обладнання.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0058_telegram_bot"
down_revision: str | None = "0057_order_sum3d_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)

    if "telegram_outbox" not in existing:
        op.create_table(
            "telegram_outbox",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("dedup_key", sa.String(200), nullable=False, unique=True),
            sa.Column("kind", sa.String(40), nullable=False, server_default=""),
            sa.Column("text", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=False), nullable=False, index=True),
            sa.Column("expires_at", sa.DateTime(timezone=False), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=False), nullable=True, index=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("next_attempt_at", sa.DateTime(timezone=False), nullable=True),
            sa.Column("last_error", sa.String(300), nullable=True),
            sa.Column("gave_up_at", sa.DateTime(timezone=False), nullable=True),
        )

    if "telegram_watch" not in existing:
        op.create_table(
            "telegram_watch",
            sa.Column("key", sa.String(120), primary_key=True),
            sa.Column("state", sa.String(20), nullable=False, server_default=""),
            sa.Column("since_at", sa.DateTime(timezone=False), nullable=False),
            sa.Column("seen_at", sa.DateTime(timezone=False), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    if "telegram_watch" in existing:
        op.drop_table("telegram_watch")
    if "telegram_outbox" in existing:
        op.drop_table("telegram_outbox")
