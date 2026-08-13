"""app/material_catalog.py — DB-backed seeding, resolution and backfill on top
of the pure classifier."""

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
import pytest

from app.material_catalog import (
    MaterialCatalogError,
    add_alias,
    add_material,
    backfill_orders,
    delete_alias,
    ensure_seeded,
    list_materials,
    load_alias_rows,
    material_id_by_name,
    resolve_material_id,
    unresolved_order_count,
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
        # a bare shade defaults to zirconia (lab convention)
        assert resolve_material_id("a2", rows, name_to_id) == zircon_id
        # a genuinely unknown value stays unresolved
        assert resolve_material_id("загадка", rows, name_to_id) is None


def test_backfill_classifies_existing_orders():
    with make_session() as session:
        session.add_all([
            Order(source="lab", material_color="моно а3.5", status="нове"),
            Order(source="lab", material_color="пмма a2", status="нове"),
            Order(source="lab", material_color="моделювання", status="нове"),
            Order(source="lab", material_color="загадка", status="нове"),  # unresolved
        ])
        session.commit()

        changed = backfill_orders(session)
        session.commit()

        assert changed == 3  # the unknown value stays unresolved
        name_to_id = material_id_by_name(session)
        by_colour = {o.material_color: o.material_id for o in session.scalars(select(Order))}
        assert by_colour["моно а3.5"] == name_to_id[ZIRCON]
        assert by_colour["пмма a2"] == name_to_id["ПММА"]
        assert by_colour["моделювання"] == name_to_id[NON_MATERIAL]
        assert by_colour["загадка"] is None


def test_backfill_is_idempotent_on_reruns():
    with make_session() as session:
        session.add(Order(source="lab", material_color="моно а3", status="нове"))
        session.commit()
        assert backfill_orders(session) == 1
        session.commit()
        # second run resolves nothing new
        assert backfill_orders(session) == 0


def test_add_alias_then_backfill_resolves_previously_unknown_colour():
    with make_session() as session:
        ensure_seeded(session)
        # a colour the seed can't classify
        order = Order(source="lab", material_color="суперкераміка x", status="нове")
        session.add(order)
        session.flush()
        assert backfill_orders(session) == 0  # still unresolved
        assert unresolved_order_count(session) == 1

        zircon_id = material_id_by_name(session)[ZIRCON]
        add_alias(session, zircon_id, "суперкераміка", "contains")

        assert backfill_orders(session, only_unresolved=False) == 1
        session.flush()
        assert order.material_id == zircon_id
        assert unresolved_order_count(session) == 0


def test_add_alias_normalizes_and_rejects_duplicate():
    with make_session() as session:
        ensure_seeded(session)
        zircon_id = material_id_by_name(session)[ZIRCON]
        alias = add_alias(session, zircon_id, "  SuperZ  ", "contains")
        assert alias.pattern == "superz"  # normalized lower/trim
        with pytest.raises(MaterialCatalogError):
            add_alias(session, zircon_id, "superz", "contains")


def test_add_alias_rejects_bad_match_type_and_empty():
    with make_session() as session:
        ensure_seeded(session)
        zircon_id = material_id_by_name(session)[ZIRCON]
        with pytest.raises(MaterialCatalogError):
            add_alias(session, zircon_id, "x", "regex")
        with pytest.raises(MaterialCatalogError):
            add_alias(session, zircon_id, "   ", "contains")


def test_delete_alias_removes_rule():
    with make_session() as session:
        ensure_seeded(session)
        zircon_id = material_id_by_name(session)[ZIRCON]
        alias = add_alias(session, zircon_id, "tempz", "contains")
        delete_alias(session, alias.id)
        rows = [r for r in load_alias_rows(session) if r.pattern == "tempz"]
        assert rows == []


def test_add_material_is_unique_and_sorts_last():
    with make_session() as session:
        ensure_seeded(session)
        others_max = max(m.sort_order for m in list_materials(session) if m.name != "Скло")
        mat = add_material(session, "Скло")
        assert mat.is_production is True
        assert mat.sort_order == others_max + 1
        with pytest.raises(MaterialCatalogError):
            add_material(session, "скло")  # case-insensitive clash
        with pytest.raises(MaterialCatalogError):
            add_material(session, "   ")


def test_list_materials_includes_aliases_in_order():
    with make_session() as session:
        ensure_seeded(session)
        materials = list_materials(session)
        assert [m.name for m in materials[:4]] == ["Цирконій", "ПММА", "СЛМ", "Титан"]
        zircon = materials[0]
        assert any(a.pattern == "моно" for a in zircon.aliases)


def test_non_material_bucket_is_not_production():
    with make_session() as session:
        ensure_seeded(session)
        non_mat = session.scalar(select(Material).where(Material.name == NON_MATERIAL))
        assert non_mat.is_production is False
        # all real materials are production
        prod = session.scalars(select(Material).where(Material.is_production.is_(True))).all()
        assert {m.name for m in prod} == {"Цирконій", "ПММА", "СЛМ", "Титан", "Віск"}
