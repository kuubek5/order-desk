"""Скорочення матеріалів для ручного вводу: `мл → mono`.

Дані, не код — власник заводить їх на екрані бібліотеки матеріалів (рішення
15.09.26). Скорочується лише слово матеріалу, колір пишеться як є, тож ~15
рядків покривають 150 комбінацій матеріал+колір. `key` — форма звіряння
(match_key), несе унікальність: неоднозначне скорочення Tab не розгортає.

Таблиця створюється порожньою: перший набір складається на екрані, не в коді.
Дивись `MaterialShortcut` і material_catalog.add_shortcut.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0066_material_shortcuts"
down_revision: str | None = "0065_machine_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "material_shortcuts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("shortcut", sa.String(length=50), nullable=False),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("expansion", sa.String(length=100), nullable=False),
    )
    op.create_index(
        "ix_material_shortcuts_key", "material_shortcuts", ["key"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_material_shortcuts_key", table_name="material_shortcuts")
    op.drop_table("material_shortcuts")
