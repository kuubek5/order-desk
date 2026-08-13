"""Top up the material catalog with the Віск category and new alias rules.

Revision ID: 0006_material_catalog_updates
Revises: 0005_add_material_catalog

Fresh installs already get these from 0005 (which seeds the current taxonomy);
this migration adds the delta to catalogs seeded by an earlier 0005. Every
insert is guarded so it's a no-op if the row already exists and never touches a
rule an operator has since edited or removed.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_material_catalog_updates"
down_revision: str | None = "0005_add_material_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# New production material discovered in the sheet.
_NEW_MATERIALS = [("Віск", True, 5)]

# (material_name, pattern, match_type) rules added since the 0005 seed.
_NEW_ALIASES = [
    ("Цирконій", "st", "token"),
    ("Цирконій", "s1", "token"),
    ("Цирконій", "tr", "token"),
    ("Цирконій", "транс", "contains"),
    ("ПММА", "hipc", "contains"),
    ("ПММА", "trinia", "contains"),
    ("Титан", "tit", "token"),
    ("Віск", "wax", "contains"),
    ("Віск", "віск", "contains"),
    ("Віск", "воск", "contains"),
    ("Не матеріал", "vtulka", "contains"),
    ("Не матеріал", "анатомія", "contains"),
]


def upgrade() -> None:
    bind = op.get_bind()
    materials = sa.table(
        "materials",
        sa.column("name", sa.String),
        sa.column("is_production", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    aliases = sa.table(
        "material_aliases",
        sa.column("material_id", sa.Integer),
        sa.column("pattern", sa.String),
        sa.column("match_type", sa.String),
        sa.column("confirmed", sa.Boolean),
    )

    def material_id(name: str):
        return bind.execute(
            sa.text("SELECT id FROM materials WHERE name = :n"), {"n": name}
        ).scalar()

    for name, is_production, sort_order in _NEW_MATERIALS:
        if material_id(name) is None:
            bind.execute(
                materials.insert().values(
                    name=name, is_production=is_production, sort_order=sort_order
                )
            )

    for material_name, pattern, match_type in _NEW_ALIASES:
        mid = material_id(material_name)
        if mid is None:
            continue
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM material_aliases WHERE pattern = :p AND match_type = :m"
            ),
            {"p": pattern, "m": match_type},
        ).first()
        if exists is None:
            bind.execute(
                aliases.insert().values(
                    material_id=mid, pattern=pattern, match_type=match_type, confirmed=True
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    for _material_name, pattern, match_type in _NEW_ALIASES:
        bind.execute(
            sa.text(
                "DELETE FROM material_aliases WHERE pattern = :p AND match_type = :m"
            ),
            {"p": pattern, "m": match_type},
        )
    for name, _is_production, _sort_order in _NEW_MATERIALS:
        bind.execute(sa.text("DELETE FROM materials WHERE name = :n"), {"n": name})
