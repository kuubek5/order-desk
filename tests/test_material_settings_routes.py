"""Material-library settings routes: admin+loopback gating and the catalog
mutations (add/delete alias, add material, reclassify). Handlers are called
directly with a fake request, same style as tests/test_update_check_route.py."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers import settings as settings_router_mod
from app.db import Base
from app.material_catalog import ensure_seeded, material_id_by_name, unresolved_order_count
from app.models import MaterialAlias, Order, User


def _db() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _admin(db: Session) -> User:
    user = User(username="admin", password_hash="x", role="адмін")
    db.add(user)
    db.commit()
    return user


def _operator(db: Session) -> User:
    user = User(username="op", password_hash="x", role="оператор")
    db.add(user)
    db.commit()
    return user


def _request(user_id, host="127.0.0.1"):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, client=SimpleNamespace(host=host))


def test_add_alias_reclassifies_and_flashes():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        db.add(Order(source="lab", material_color="небула x", status="нове"))
        db.commit()
        assert unresolved_order_count(db) == 1

        zircon_id = material_id_by_name(db)["Цирконій"]
        req = _request(admin.id)
        resp = settings_router_mod.add_material_alias(req, material_id=zircon_id, pattern="небула", match_type="contains", db=db)

        assert resp.status_code == 303
        assert req.session["materials_flash"]["kind"] == "success"
        assert unresolved_order_count(db) == 0
        order = db.scalar(select(Order))
        assert order.material_id == zircon_id


def test_add_alias_duplicate_flashes_error():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        zircon_id = material_id_by_name(db)["Цирконій"]
        req = _request(admin.id)
        # 'моно' is already a seeded zircon alias
        settings_router_mod.add_material_alias(req, material_id=zircon_id, pattern="моно", match_type="contains", db=db)
        assert req.session["materials_flash"]["kind"] == "error"


def test_delete_alias_reevaluates_orders():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        # order resolved only via the 'kappa' PMMA alias
        db.add(Order(source="lab", material_color="kappa", status="нове"))
        db.commit()
        # first classify it
        settings_router_mod.reclassify_materials(_request(admin.id), db=db)
        assert unresolved_order_count(db) == 0

        kappa = db.scalar(select(MaterialAlias).where(MaterialAlias.pattern == "kappa"))
        settings_router_mod.remove_material_alias(kappa.id, _request(admin.id), db=db)
        # with the rule gone, the order is unresolved again
        assert unresolved_order_count(db) == 1


def test_create_material_adds_category():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        resp = settings_router_mod.create_material(_request(admin.id), name="Скло", is_production="on", db=db)
        assert resp.status_code == 303
        assert "Скло" in material_id_by_name(db)


def test_operator_is_forbidden():
    with _db() as db:
        op = _operator(db)
        ensure_seeded(db)
        zircon_id = material_id_by_name(db)["Цирконій"]
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.add_material_alias(_request(op.id), material_id=zircon_id, pattern="x", match_type="contains", db=db)
        assert exc.value.status_code == 403


def test_non_loopback_is_forbidden():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.reclassify_materials(_request(admin.id, host="10.0.0.5"), db=db)
        assert exc.value.status_code == 403
