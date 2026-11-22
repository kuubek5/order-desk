"""Vivid — цирконій (власник 22.11.26).

Звірка «Виробітку» з ручною таблицею за листопад: роботи з матеріалом
«Vivid 4d A3» лишались без матеріалу, а рядок без матеріалу не рахується ніде
(12.11.26: пошта ZrO 117 замість 131 — рівно 14 одиниць двох таких рядків).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.material_catalog import backfill_orders, ensure_seeded
from app.material_classifier import PMMA, ZIRCON, classify_material
from app.models import Material, MaterialAlias, Order
from app.services import disc_suggest
from app.services.vyrobitok import MATERIAL_KEY_BY_NAME

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "0108_seed_vivid_zirconia_aliases.py"
)
VIVID = ("vivid", "вівід", "вивид")


@pytest.mark.parametrize("raw", ["Vivid 4d A3", "вівід 4д а3", "VIVID A2", "вивид а3.5", "vivid"])
def test_vivid_is_classified_as_zirconia(raw):
    assert classify_material(raw) == ZIRCON


def test_other_materials_are_not_touched():
    assert classify_material("pmma a3") == PMMA
    assert classify_material("mono a3") == ZIRCON
    # Колонка «Виробітку» для цирконію — саме та, куди тепер ляжуть ці роботи.
    assert MATERIAL_KEY_BY_NAME[ZIRCON] == "zr"


def test_disc_hint_keeps_vivid_apart_from_monolith():
    """Без окремої лінійки робота Vivid, ставши цирконієм, отримала б у підказці
    будь-який диск потрібного кольору — тобто Monolith."""
    want = disc_suggest.want_of("Vivid 4d A3")
    assert (want.family, want.line, set(want.shades)) == (ZIRCON, "vivid", {"a3"})
    assert want.label() == "Vivid A3"
    monolith = disc_suggest.DiscFile(
        path=Path("zr16_16-Monolith-A3-x5.blk"), stem="zr16_16-Monolith-A3-x5", height=16,
        family=ZIRCON, line="monolith", shades=frozenset({"a3"}),
    )
    vivid = disc_suggest.DiscFile(
        path=Path("zr16_16-Vivid-A3-x1.blk"), stem="zr16_16-Vivid-A3-x1", height=16,
        family=ZIRCON, line="vivid", shades=frozenset({"a3"}),
    )
    assert disc_suggest._matches(vivid, want) and not disc_suggest._matches(monolith, want)
    # Monolith-робота, як і досі, не отримує диск Vivid.
    assert not disc_suggest._matches(vivid, disc_suggest.want_of("mono a3"))


def _old_database() -> Session:
    """База, засіяна ДО цієї правки: довідник є, синонімів Vivid у ньому немає."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    ensure_seeded(db)
    db.query(MaterialAlias).filter(MaterialAlias.pattern.in_(VIVID)).delete(synchronize_session=False)
    db.commit()
    return db


def _run(db: Session, name: str) -> None:
    spec = importlib.util.spec_from_file_location("migration_0108", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = db.connection()
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, name)()
    db.commit()


def test_migration_gives_an_existing_database_the_aliases_and_backfill_counts_the_work():
    db = _old_database()
    order = Order(source="email", sheet_tab="12.11.26", row_number=106, status="нове",
                  material_color="Vivid 4d A3", quantity="8")
    db.add(order)
    db.commit()
    assert backfill_orders(db) == 0 and order.material_id is None, "до міграції — без матеріалу"

    _run(db, "upgrade")
    zircon_id = db.query(Material).filter_by(name=ZIRCON).one().id
    rows = db.query(MaterialAlias).filter(MaterialAlias.pattern.in_(VIVID)).all()
    assert sorted(r.pattern for r in rows) == sorted(VIVID)
    assert all(r.material_id == zircon_id and r.match_type == "contains" for r in rows)

    # Те саме робить старт застосунку (`web.py`): роботи без матеріалу — перекласифікувати.
    assert backfill_orders(db) == 1
    db.commit()
    assert order.material_id == zircon_id

    # Повторний прогін нічого не дублює.
    _run(db, "upgrade")
    assert db.query(MaterialAlias).filter(MaterialAlias.pattern.in_(VIVID)).count() == len(VIVID)

    _run(db, "downgrade")
    assert db.query(MaterialAlias).filter(MaterialAlias.pattern.in_(VIVID)).count() == 0


def test_migration_skips_a_database_without_a_catalog():
    """Чиста база: довідник засіє `ensure_seeded` уже з новими синонімами."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    _run(db, "upgrade")
    assert db.execute(text("SELECT COUNT(*) FROM material_aliases")).scalar() == 0
    ensure_seeded(db)
    db.commit()
    assert db.query(MaterialAlias).filter(MaterialAlias.pattern.in_(VIVID)).count() == len(VIVID)
