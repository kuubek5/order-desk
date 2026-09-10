"""Учасники Telegram-бота й одноразові запрошення.

Revision ID: 0059_telegram_members
Revises: 0058_telegram_bot

Власник попросив (10.09.26) зручно додавати до бота інших людей. Приходять
вони лише за одноразовим посиланням `t.me/<бот>?start=<код>`, яке власник
створює в налаштуваннях: сторонньому без коду бот, як і досі, мовчить.

`telegram_outbox.chat_id` — адресат рядка: сповіщення тепер ідуть кожному
учаснику окремим рядком, щоб недосяжний один не тримав чергу решти. NULL —
власник (рядки, створені 0058 до появи учасників).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0059_telegram_members"
down_revision: str | None = "0058_telegram_bot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)

    if "telegram_members" not in existing:
        op.create_table(
            "telegram_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chat_id", sa.String(40), nullable=False, unique=True),
            sa.Column("name", sa.String(200), nullable=False, server_default=""),
            sa.Column("username", sa.String(100), nullable=True),
            sa.Column("label", sa.String(120), nullable=False, server_default=""),
            sa.Column("notify", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("joined_at", sa.DateTime(timezone=False), nullable=False),
            sa.Column("invited_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=False), nullable=True),
        )

    if "telegram_invites" not in existing:
        op.create_table(
            "telegram_invites",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(64), nullable=False, unique=True),
            sa.Column("label", sa.String(120), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=False), nullable=False),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("used_at", sa.DateTime(timezone=False), nullable=True),
            sa.Column("used_by_chat", sa.String(40), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=False), nullable=True),
        )

    if "telegram_outbox" in existing and "chat_id" not in _columns(bind, "telegram_outbox"):
        op.add_column("telegram_outbox", sa.Column("chat_id", sa.String(40), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    if "telegram_outbox" in existing and "chat_id" in _columns(bind, "telegram_outbox"):
        with op.batch_alter_table("telegram_outbox") as batch:
            batch.drop_column("chat_id")
    if "telegram_invites" in existing:
        op.drop_table("telegram_invites")
    if "telegram_members" in existing:
        op.drop_table("telegram_members")
