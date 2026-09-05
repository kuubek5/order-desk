"""Табель «Виробіток»: місячні налаштування + клітинки.

Revision ID: 0043_vyrobitok_tables
Revises: 0042_order_issue_source

Новий розділ місячного обліку виготовлених одиниць (окремо від «Статистики»).
Дві таблиці:

* `vyrobitok_months` — курс валюти й склад зміни на місяць. В ODS вони були
  вбиті у формулу (`×52/4`); тут — поля, як вимагає власник.
* `vyrobitok_cells` — число в клітинці (день × колонка). `override_value` тримає
  правку оператора, `auto_value` — знімок порахованого CRM, щоб минулий місяць
  не став 0, коли лаба почистить старі вкладки й синк заархівує ті роботи.

Обидві таблиці нові, тож на чистій базі їх зробить `create_all`; ця міграція
потрібна лише наявним інсталяціям.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0043_vyrobitok_tables"
down_revision: str | None = "0042_order_issue_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vyrobitok_months",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False, index=True),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("kurs", sa.String(length=20), nullable=False, server_default="52"),
        sa.Column(
            "people_count", sa.Integer(), nullable=False, server_default="5"
        ),
        sa.Column("rate_override", sa.String(length=20), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=False), server_default=sa.func.now()
        ),
        sa.UniqueConstraint("year", "month", name="uq_vyrobitok_month"),
    )
    op.create_table(
        "vyrobitok_cells",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False, index=True),
        sa.Column("col_key", sa.String(length=20), nullable=False),
        sa.Column("auto_value", sa.Integer(), nullable=True),
        sa.Column("override_value", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=False), server_default=sa.func.now()
        ),
        sa.UniqueConstraint("day", "col_key", name="uq_vyrobitok_cell"),
    )


def downgrade() -> None:
    op.drop_table("vyrobitok_cells")
    op.drop_table("vyrobitok_months")
