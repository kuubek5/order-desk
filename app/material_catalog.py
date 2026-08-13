"""Bridge between the pure classifier (app/material_classifier.py) and the DB
catalog (Material / MaterialAlias).

The migration seeds the catalog for real installs; ensure_seeded() covers the
create_all path (tests, first boot) idempotently. resolve_material_id() turns a
raw colour string into a Material id using the DB aliases, and backfill_orders()
classifies existing rows after the feature ships.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.material_classifier import (
    AliasRow,
    SEED_ALIASES,
    SEED_MATERIALS,
    classify_material,
)
from app.models import Material, MaterialAlias, Order


def ensure_seeded(session: Session) -> None:
    """Seed the catalog from the classifier taxonomy if it's empty. Idempotent:
    a no-op once materials exist, so it's safe to call on every boot and in
    tests that build the schema via create_all (which the migration seed skips)."""
    if session.execute(select(Material.id).limit(1)).first() is not None:
        return
    id_by_name: dict[str, int] = {}
    for name, is_production, sort_order in SEED_MATERIALS:
        material = Material(name=name, is_production=is_production, sort_order=sort_order)
        session.add(material)
        session.flush()
        id_by_name[name] = material.id
    for material_name, aliases in SEED_ALIASES.items():
        for pattern, match_type in aliases:
            session.add(
                MaterialAlias(
                    material_id=id_by_name[material_name],
                    pattern=pattern,
                    match_type=match_type,
                    confirmed=True,
                )
            )
    session.flush()


def load_alias_rows(session: Session) -> list[AliasRow]:
    """All alias rules as classifier AliasRow objects (pattern, match_type,
    material name). Load once per sync/backfill, not per order."""
    rows = session.execute(
        select(MaterialAlias.pattern, MaterialAlias.match_type, Material.name).join(
            Material, MaterialAlias.material_id == Material.id
        )
    ).all()
    return [AliasRow(pattern=p, match_type=mt, material=name) for p, mt, name in rows]


def material_id_by_name(session: Session) -> dict[str, int]:
    return {
        name: mid
        for mid, name in session.execute(select(Material.id, Material.name)).all()
    }


def resolve_material_id(
    raw: str | None,
    alias_rows: list[AliasRow],
    name_to_id: dict[str, int],
) -> int | None:
    """Classify `raw` and map the resulting category name to its Material id, or
    None if unresolved. Caller loads alias_rows/name_to_id once and reuses them."""
    name = classify_material(raw, alias_rows)
    if name is None:
        return None
    return name_to_id.get(name)


def backfill_orders(session: Session, *, only_unresolved: bool = True) -> int:
    """Classify existing orders' material_id from their material_color. Returns
    the number of orders newly assigned a material. only_unresolved=True skips
    orders that already have a material (idempotent re-runs); pass False to
    re-classify everything (e.g. after adding aliases)."""
    ensure_seeded(session)
    alias_rows = load_alias_rows(session)
    name_to_id = material_id_by_name(session)

    stmt = select(Order)
    if only_unresolved:
        stmt = stmt.where(Order.material_id.is_(None))
    orders = session.execute(stmt).scalars().all()

    changed = 0
    for order in orders:
        resolved = resolve_material_id(order.material_color, alias_rows, name_to_id)
        if resolved is not None and resolved != order.material_id:
            order.material_id = resolved
            changed += 1
    return changed
