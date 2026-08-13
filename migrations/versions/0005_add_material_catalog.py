"""Add material catalog (materials + material_aliases) and orders.material_id.

Revision ID: 0005_add_material_catalog
Revises: 0004_add_client_table

Creates the material category catalog and its alias rules, seeds them from
app/material_classifier.py's taxonomy, and adds the nullable orders.material_id
resolved by the classifier. Backfilling existing orders' material_id is a
separate application step (see app/material_catalog.backfill_orders), not part
of the schema migration.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

from app.material_classifier import SEED_ALIASES, SEED_MATERIALS


revision: str = "0005_add_material_catalog"
down_revision: str | None = "0004_add_client_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "materials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_production", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "material_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False, server_default="contains"),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_material_aliases_material_id", "material_aliases", ["material_id"])
    op.create_index("ix_material_aliases_pattern", "material_aliases", ["pattern"])

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("material_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_orders_material_id", "materials", ["material_id"], ["id"]
        )
    op.create_index("ix_orders_material_id", "orders", ["material_id"])

    # Seed catalog from the classifier taxonomy so a fresh install classifies
    # out of the box; operators extend it later via MaterialAlias.
    bind = op.get_bind()
    materials_tbl = sa.table(
        "materials",
        sa.column("name", sa.String),
        sa.column("is_production", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    for name, is_production, sort_order in SEED_MATERIALS:
        bind.execute(
            materials_tbl.insert().values(
                name=name, is_production=is_production, sort_order=sort_order
            )
        )

    name_to_id = {
        row[1]: row[0]
        for row in bind.execute(sa.text("SELECT id, name FROM materials")).fetchall()
    }
    alias_tbl = sa.table(
        "material_aliases",
        sa.column("material_id", sa.Integer),
        sa.column("pattern", sa.String),
        sa.column("match_type", sa.String),
        sa.column("confirmed", sa.Boolean),
    )
    for material_name, aliases in SEED_ALIASES.items():
        material_id = name_to_id[material_name]
        for pattern, match_type in aliases:
            bind.execute(
                alias_tbl.insert().values(
                    material_id=material_id,
                    pattern=pattern,
                    match_type=match_type,
                    confirmed=True,
                )
            )


def downgrade() -> None:
    op.drop_index("ix_orders_material_id", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("fk_orders_material_id", type_="foreignkey")
        batch.drop_column("material_id")
    op.drop_index("ix_material_aliases_pattern", table_name="material_aliases")
    op.drop_index("ix_material_aliases_material_id", table_name="material_aliases")
    op.drop_table("material_aliases")
    op.drop_table("materials")
