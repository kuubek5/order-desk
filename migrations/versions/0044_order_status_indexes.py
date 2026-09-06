"""Індекси під Чергу й Видачу: orders.status і (client_name, status).

Revision ID: 0044_order_status_indexes
Revises: 0043_vyrobitok_tables

Таблиця `orders` не чиститься за задумом: retention лише переносить роботи в
Архів (`archived_at`), рядки лишаються назавжди. При ~92 рядках за робочий день
це десятки тисяч записів уже за рік, а обидва найгарячіші екрани фільтрують
саме за статусом:

* Черга — незавершені роботи, полл кожні 15 с;
* Видача — невидані роботи, згруповані ПО КЛІЄНТУ.

Без індексу кожен такий запит — повний перебір таблиці. `ix_orders_status`
закриває перший випадок, складений `ix_orders_client_name_status` — другий
(спершу `client_name`: рівність по клієнту відсікає майже все, тоді як самих
значень статусу одиниці, і індекс лише по ньому для вибірки клієнта марний).

Міграція лише додає індекси — дані не змінюються, схема колонок теж, тож
`_matches_models` у app/schema.py її не «побачить». Саме тому вона й потрібна
окремо: наявні бази проштамповані на 0043 і нових індексів самі не отримають
(`create_all` створює їх лише на ЧИСТІЙ базі).

`if_not_exists` не використовуємо (alembic на sqlite його не проксіює) — замість
цього імена індексів нові й не перетинаються з наявними.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0044_order_status_indexes"
down_revision: str | None = "0043_vyrobitok_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_client_name_status", "orders", ["client_name", "status"])


def downgrade() -> None:
    op.drop_index("ix_orders_client_name_status", table_name="orders")
    op.drop_index("ix_orders_status", table_name="orders")
