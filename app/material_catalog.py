"""Bridge between the pure classifier (app/material_classifier.py) and the DB
catalog (Material / MaterialAlias).

The migration seeds the catalog for real installs; ensure_seeded() covers the
create_all path (tests, first boot) idempotently. resolve_material_id() turns a
raw colour string into a Material id using the DB aliases, and backfill_orders()
classifies existing rows after the feature ships.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.material_classifier import (
    AliasRow,
    SEED_ALIASES,
    SEED_MATERIALS,
    classify_material,
    normalize_material,
)
from app.models import Material, MaterialAlias, Order

VALID_MATCH_TYPES = ("contains", "token")


class MaterialCatalogError(ValueError):
    """User-facing, safe validation error for catalog edits."""


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


def list_materials(session: Session) -> list[Material]:
    """Materials in display order, aliases eager-loaded for the settings screen."""
    return list(
        session.execute(
            select(Material)
            .options(selectinload(Material.aliases))
            .order_by(Material.sort_order, Material.name)
        ).scalars()
    )


def unresolved_order_count(session: Session) -> int:
    """How many orders have a colour but no resolved material — the size of the
    'needs a rule' backlog the admin is working down."""
    return session.scalar(
        select(func.count(Order.id)).where(
            Order.material_id.is_(None), Order.material_color.isnot(None)
        )
    ) or 0


def add_alias(session: Session, material_id: int, pattern: str, match_type: str) -> MaterialAlias:
    """Add one alias rule after validating and normalizing it. Raises
    MaterialCatalogError on bad input or a duplicate."""
    if match_type not in VALID_MATCH_TYPES:
        raise MaterialCatalogError("Невідомий тип зіставлення.")
    normalized = normalize_material(pattern)
    if not normalized:
        raise MaterialCatalogError("Порожній шаблон.")
    if len(normalized) > 200:
        raise MaterialCatalogError("Шаблон задовгий.")
    material = session.get(Material, material_id)
    if material is None:
        raise MaterialCatalogError("Матеріал не знайдено.")
    exists = session.scalar(
        select(MaterialAlias.id).where(
            MaterialAlias.pattern == normalized, MaterialAlias.match_type == match_type
        )
    )
    if exists is not None:
        raise MaterialCatalogError(f"Правило «{normalized}» вже існує.")
    alias = MaterialAlias(
        material_id=material_id, pattern=normalized, match_type=match_type, confirmed=True
    )
    session.add(alias)
    session.flush()
    return alias


def delete_alias(session: Session, alias_id: int) -> None:
    alias = session.get(MaterialAlias, alias_id)
    if alias is not None:
        session.delete(alias)
        session.flush()


def add_material(session: Session, name: str, *, is_production: bool = True) -> Material:
    """Create a new material category. Name must be non-empty and unique
    (case-insensitive). New rows sort after existing ones."""
    clean = (name or "").strip()
    if not clean:
        raise MaterialCatalogError("Порожня назва матеріалу.")
    if len(clean) > 100:
        raise MaterialCatalogError("Назва матеріалу задовга.")
    # Case-insensitive uniqueness compared in Python: SQLite's lower() only
    # folds ASCII, so a Cyrillic clash (Скло vs скло) would slip past func.lower.
    existing_names = session.scalars(select(Material.name)).all()
    if any(name.lower() == clean.lower() for name in existing_names):
        raise MaterialCatalogError(f"Матеріал «{clean}» вже існує.")
    max_sort = session.scalar(select(func.max(Material.sort_order))) or 0
    material = Material(name=clean, is_production=is_production, sort_order=max_sort + 1)
    session.add(material)
    session.flush()
    return material


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
