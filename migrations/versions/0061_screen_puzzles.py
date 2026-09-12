"""Скринька невідомих екранів: кадр, якого зчитувач не зрозумів.

Revision ID: 0061_screen_puzzles
Revises: 0060_disc_orders

Читач екранів мовчить при найменшому сумніві — і це правильно, але мовчання
нікуди не веде: кадр на диску лежить рівно один на пристрій і перезаписується
кожні кілька секунд, тож рідкісний екран (аварія вночі, незнайомий діалог) до
розбору не доживав ніколи.

Рядок тут — на ЕКРАН І ПРИЧИНУ, не на кадр (на одному кадрі читач може
спіткнутись по-різному, і це різні питання до людини). Тотожність визначає `signature` —
мініатюра 64×48 відтінків сірого, яку порівнюють за СЕРЕДНЬОЮ різницею з
порогом (той самий спосіб, що у відборі калібрувальних кадрів верстатів).
Хеш для цього не годиться: на екрані постійно міняються годинник, відсоток і
назва програми, і кожен кадр ставав би новим рядком — 600 на годину на
пристрій. `fingerprint` лишається коротким імʼям для файлу й унікальності.

Підпис людини (`label`) — дані, не правило: зону, еталон чи правило статусу з
нього роблять у репозиторії з тестом. `dismissed` — «неважливо, не питай
більше»: рядок лишається з лічильником, але першим іде на витіснення.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0061_screen_puzzles"
down_revision: str | None = "0060_disc_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if "screen_puzzles" in _tables(bind):
        return
    op.create_table(
        "screen_puzzles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False, index=True),
        sa.Column("device_key", sa.String(64), nullable=False, index=True),
        sa.Column("device_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("fingerprint", sa.String(32), nullable=False, index=True),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("reason", sa.String(32), nullable=False, index=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("frame_file", sa.String(160), nullable=True),
        sa.Column("zone_file", sa.String(160), nullable=True),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=False), nullable=False, index=True),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("labeled_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("labeled_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        # Той самий екран того самого пристрою — один рядок. Унікальність саме
        # в схемі: захоплення йде з фонових потоків опитування, і двоє можуть
        # принести один відпечаток одночасно.
        sa.UniqueConstraint(
            "device_key", "fingerprint", "reason", name="uq_screen_puzzle_device_fp"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "screen_puzzles" in _tables(bind):
        op.drop_table("screen_puzzles")
