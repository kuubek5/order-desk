"""Seed the temporary-crown → ПММА material aliases on existing installs.

Revision ID: 0016_seed_pmma_temp_aliases
Revises: 0015_add_handled_link_refs

app/material_classifier.py's SEED_ALIASES gained «врім'янка»/«temp» → ПММА so
the mail triage recognises the product wording clients use for PMMA. But the
material catalog is only seeded when EMPTY (migration 0005 / ensure_seeded), so
installs that already ran 0005 never pick up aliases added to the seed later.
This migration inserts exactly those new PMMA aliases, idempotently (skips any
already present by pattern+match_type), so existing DBs match a fresh install.
Purely additive data; downgrade removes only the rows it added.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0016_seed_pmma_temp_aliases"
down_revision: str | None = "0015_add_handled_link_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The aliases added to SEED_ALIASES[PMMA] after 0005 shipped.
_NEW_PMMA_ALIASES: list[tuple[str, str]] = [
    ("врім", "contains"), ("врем", "contains"),
    ("тимчасов", "contains"), ("временн", "contains"),
    ("temp", "contains"),
]


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT id FROM materials WHERE name = :name"), {"name": "ПММА"}
    ).fetchone()
    if row is None:
        # Catalog not seeded yet (fresh create_all path) — ensure_seeded will
        # add these from the up-to-date SEED_ALIASES, nothing to backfill.
        return
    pmma_id = row[0]
    for pattern, match_type in _NEW_PMMA_ALIASES:
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
                {"mid": pmma_id, "p": pattern, "m": match_type},
            )


def downgrade() -> None:
    bind = op.get_bind()
    for pattern, match_type in _NEW_PMMA_ALIASES:
        bind.execute(
            sa.text("DELETE FROM material_aliases WHERE pattern = :p AND match_type = :m"),
            {"p": pattern, "m": match_type},
        )
