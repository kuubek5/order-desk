"""Замовлення дисків на склад окремою таблицею; склад як отримувач у боті.

Revision ID: 0060_disc_orders
Revises: 0059_telegram_members

Екран «Нові диски» (10.09.26) замінив розділ «Заготовки». Замовлення тепер
має те, що з рядків дисків не виводиться: дописане від руки (фрези,
полірувальні диски), спосіб відправки (Telegram / без відправки) і стан
«скасовано» — скасований запис лишається в історії закресленим, хоча його
диски повертаються в «чекають». Тому:

* `cam_blank_orders` — одне замовлення; `cam_blanks.order_id` — у якому
  замовленні пішов диск;
* ДАНІ: кожна наявна пачка `ordered_at` (одне натискання старої кнопки
  «Замовлено») стає записом `cam_blank_orders` з `via='manual'`, а її диски
  отримують `order_id`. Точка відліку (`ordered_at == first_seen_at`, перше
  читання теки) замовленням НЕ є і не переноситься — інакше в історії
  зʼявилось би «замовлення на 19 тисяч дисків», якого не було. Текст і
  підпис змін у перенесених записів порожні: екран складає їх із
  прив'язаних дисків (формат рядка з тих пір змінився, і знімок у старому
  форматі був би неправдою про те, як виглядає замовлення тепер);
* `telegram_members.role` / `telegram_invites.role` — «warehouse» для
  складу: отримує лише замовлення дисків, без меню й сповіщень печей;
* `telegram_outbox.message_id` / `edit_of_id` — щоб при скасуванні бот
  РЕДАГУВАВ те саме повідомлення складу, а не слав нове.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0060_disc_orders"
down_revision: str | None = "0059_telegram_members"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _add(bind, table: str, column: sa.Column) -> None:
    if table in _tables(bind) and column.name not in _columns(bind, table):
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()

    if "cam_blank_orders" not in _tables(bind):
        op.create_table(
            "cam_blank_orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=False), nullable=False, index=True),
            sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("via", sa.String(20), nullable=False, server_default="manual"),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("text", sa.Text(), nullable=False, server_default=""),
            sa.Column("disc_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("label", sa.String(120), nullable=False, server_default=""),
            sa.Column("cancelled_at", sa.DateTime(timezone=False), nullable=True),
            sa.Column("cancelled_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        )

    if "cam_blanks" in _tables(bind) and "order_id" not in _columns(bind, "cam_blanks"):
        # SQLite не вміє ALTER ADD CONSTRAINT, тож FK іде разом із колонкою
        # через batch (копія таблиці). Таблиця невелика — десятки тисяч рядків
        # імен файлів, це секунди.
        with op.batch_alter_table("cam_blanks") as batch:
            batch.add_column(sa.Column("order_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_cam_blanks_order_id", "cam_blank_orders", ["order_id"], ["id"]
            )
            batch.create_index("ix_cam_blanks_order_id", ["order_id"])

    _add(bind, "telegram_members", sa.Column("role", sa.String(20), nullable=False, server_default=""))
    _add(bind, "telegram_invites", sa.Column("role", sa.String(20), nullable=False, server_default=""))
    _add(bind, "telegram_outbox", sa.Column("message_id", sa.Integer(), nullable=True))
    if "telegram_outbox" in _tables(bind) and "edit_of_id" not in _columns(bind, "telegram_outbox"):
        op.add_column("telegram_outbox", sa.Column("edit_of_id", sa.Integer(), nullable=True))
        op.create_index("ix_telegram_outbox_edit_of_id", "telegram_outbox", ["edit_of_id"])

    _backfill_orders(bind)


def _backfill_orders(bind) -> None:
    """Пачки старої кнопки «Замовлено» → записи `cam_blank_orders`.

    Пачка = усі диски з однаковим `ordered_at` (один виклик `mark_ordered`
    ставив один час). Ідемпотентно: береться лише те, що ще без `order_id`,
    тож повторний прогін не задвоїть історію.
    """
    if "cam_blanks" not in _tables(bind) or "cam_blank_orders" not in _tables(bind):
        return
    batches = bind.execute(
        sa.text(
            "SELECT ordered_at, COUNT(*) FROM cam_blanks "
            "WHERE ordered_at IS NOT NULL AND ordered_at != first_seen_at "
            "AND order_id IS NULL "
            "GROUP BY ordered_at ORDER BY ordered_at"
        )
    ).all()
    for stamp, count in batches:
        result = bind.execute(
            sa.text(
                "INSERT INTO cam_blank_orders (created_at, via, note, text, disc_count, label) "
                "VALUES (:at, 'manual', '', '', :n, '')"
            ),
            {"at": stamp, "n": int(count)},
        )
        order_id = result.lastrowid
        bind.execute(
            sa.text(
                "UPDATE cam_blanks SET order_id = :id "
                "WHERE ordered_at = :at AND ordered_at != first_seen_at AND order_id IS NULL"
            ),
            {"id": order_id, "at": stamp},
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    if "telegram_outbox" in existing:
        columns = _columns(bind, "telegram_outbox")
        with op.batch_alter_table("telegram_outbox") as batch:
            if "edit_of_id" in columns:
                batch.drop_index("ix_telegram_outbox_edit_of_id")
                batch.drop_column("edit_of_id")
            if "message_id" in columns:
                batch.drop_column("message_id")
    for table in ("telegram_invites", "telegram_members"):
        if table in existing and "role" in _columns(bind, table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("role")
    if "cam_blanks" in existing and "order_id" in _columns(bind, "cam_blanks"):
        with op.batch_alter_table("cam_blanks") as batch:
            batch.drop_index("ix_cam_blanks_order_id")
            batch.drop_constraint("fk_cam_blanks_order_id", type_="foreignkey")
            batch.drop_column("order_id")
    if "cam_blank_orders" in existing:
        op.drop_table("cam_blank_orders")
