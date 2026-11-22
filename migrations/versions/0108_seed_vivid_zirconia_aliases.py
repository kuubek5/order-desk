"""Seed the Vivid → Цирконій material aliases on existing installs.

Revision ID: 0108_seed_vivid_zirconia_aliases
Revises: 0107_selection_journal

Власник 22.11.26: «Vivid — це цирконій». Звірка «Виробітку» з ручною таблицею
за листопад показала, що роботи з матеріалом «Vivid 4d A3» / «вівід 4д а3»
лишались без матеріалу й тому не рахувались НІДЕ (12.11.26: пошта ZrO 117
замість 131 — рівно 14 одиниць двох таких рядків).

`material_classifier.SEED_ALIASES` доповнено, але довідник засівається лише
порожнім (0005 / `ensure_seeded`), тож наявні бази нових синонімів не
отримують — ця міграція додає їх ідемпотентно (пропускає ті, що вже є за
шаблоном і типом). Самі роботи перекласифікує `backfill_orders` на старті
застосунку (`web.py`, лише ті, що досі без матеріалу).

Лише дані; downgrade прибирає рівно ці рядки.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0108_seed_vivid_zirconia_aliases"
down_revision: str | None = "0107_selection_journal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Синоніми, додані до SEED_ALIASES[Цирконій] 22.11.26.
_NEW_ZIRCON_ALIASES: list[tuple[str, str]] = [
    ("vivid", "contains"), ("вівід", "contains"), ("вивид", "contains"),
]


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT id FROM materials WHERE name = :name"), {"name": "Цирконій"}
    ).fetchone()
    if row is None:
        # Довідник ще не засіяний (чиста база) — `ensure_seeded` додасть ці
        # синоніми з актуального SEED_ALIASES, досівати нічого.
        return
    zircon_id = row[0]
    for pattern, match_type in _NEW_ZIRCON_ALIASES:
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM material_aliases WHERE pattern = :p AND match_type = :m"
            ),
            {"p": pattern, "m": match_type},
        ).fetchone()
        if exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO material_aliases (material_id, pattern, match_type, confirmed) "
                    "VALUES (:mid, :p, :m, 1)"
                ),
                {"mid": zircon_id, "p": pattern, "m": match_type},
            )


def downgrade() -> None:
    bind = op.get_bind()
    for pattern, match_type in _NEW_ZIRCON_ALIASES:
        bind.execute(
            sa.text("DELETE FROM material_aliases WHERE pattern = :p AND match_type = :m"),
            {"p": pattern, "m": match_type},
        )
