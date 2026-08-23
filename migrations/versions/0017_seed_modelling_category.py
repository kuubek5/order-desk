"""Seed the «моделювання» mail-filter category on existing installs.

Revision ID: 0017_seed_modelling_category
Revises: 0016_seed_pmma_temp_aliases

Clients sometimes email STL files for MODELLING — work the lab hands to its
technicians and does not mill. Those letters belong in the «Відфільтровані» tab,
out of the milling queue, same as 3D-друк. This adds «моделювання» to the
editable filter categories (migration 0010 seeded the first four) so it's there
to pick out of the box. Idempotent: skips it if an admin already added the name.
The trigger keyword/sender rule is NOT seeded — the admin adds it (from triage
or the filters settings) so auto-filtering only starts on their say-so.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0017_seed_modelling_category"
down_revision: str | None = "0016_seed_pmma_temp_aliases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "моделювання"


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT 1 FROM mail_filter_categories WHERE name = :n"), {"n": _NAME}
    ).fetchone()
    if exists is None:
        bind.execute(
            sa.text("INSERT INTO mail_filter_categories (name) VALUES (:n)"), {"n": _NAME}
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM mail_filter_categories WHERE name = :n"), {"n": _NAME}
    )
