"""app/material_catalog.py — DB-backed seeding, resolution and backfill on top
of the pure classifier."""

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.material_catalog import (
    backfill_orders,
    ensure_seeded,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
)
from app.material_classifier import SEED_ALIASES, SEED_MATERIALS, NON_MATERIAL, ZIRCON
from app.models import Material, MaterialAlias, Order


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_ensure_seeded_populates_catalog_once():
    with make_session() as session:
        ensure_seeded(session)
        ensure_seeded(session)  # idempotent — second call adds nothing

        materials = session.scalar(select(func.count(Material.id)))
        aliases = session.scalar(select(func.count(MaterialAlias.id)))
        assert materials == len(SEED_MATERIALS)
        assert aliases == sum(len(v) for v in SEED_ALIASES.values())


def test_resolve_material_id_maps_colour_to_catalog():
    with make_session() as session:
        ensure_seeded(session)
        rows = load_alias_rows(session)
        name_to_id = material_id_by_name(session)

        zircon_id = name_to_id[ZIRCON]
        assert resolve_material_id("моно а3", rows, name_to_id) == zircon_id
        assert resolve_material_id("800", rows, name_to_id) == zircon_id
        assert resolve_material_id("пмма a2", rows, name_to_id) == name_to_id["ПММА"]
        assert resolve_material_id("титан корея", rows, name_to_id) == name_to_id["Титан"]
        # bare shade is unresolved
        assert resolve_material_id("a2", rows, name_to_id) is None


def test_backfill_classifies_existing_orders():
    with make_session() as session:
        session.add_all([
            Order(source="lab", material_color="моно а3.5", status="нове"),
            Order(source="lab", material_color="пмма a2", status="нове"),
            Order(source="lab", material_color="моделювання", status="нове"),
            Order(source="lab", material_color="a2", status="нове"),  # unresolved
        ])
        session.commit()

        changed = backfill_orders(session)
        session.commit()

        assert changed == 3  # the bare shade stays unresolved
        name_to_id = material_id_by_name(session)
        by_colour = {o.material_color: o.material_id for o in session.scalars(select(Order))}
        assert by_colour["моно а3.5"] == name_to_id[ZIRCON]
        assert by_colour["пмма a2"] == name_to_id["ПММА"]
        assert by_colour["моделювання"] == name_to_id[NON_MATERIAL]
        assert by_colour["a2"] is None


def test_backfill_is_idempotent_on_reruns():
    with make_session() as session:
        session.add(Order(source="lab", material_color="моно а3", status="нове"))
        session.commit()
        assert backfill_orders(session) == 1
        session.commit()
        # second run resolves nothing new
        assert backfill_orders(session) == 0


def test_non_material_bucket_is_not_production():
    with make_session() as session:
        ensure_seeded(session)
        non_mat = session.scalar(select(Material).where(Material.name == NON_MATERIAL))
        assert non_mat.is_production is False
        # all real materials are production
        prod = session.scalars(select(Material).where(Material.is_production.is_(True))).all()
        assert {m.name for m in prod} == {"Цирконій", "ПММА", "СЛМ", "Титан"}
