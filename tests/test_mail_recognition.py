"""Mail material recognition (A/C): editable material synonyms flowing into the
triage guess, and the default-material fallback + its settings route. Route
handlers are called directly with a fake request, same style as
tests/test_material_settings_routes.py.

3D-друк / моделювання recognition is NOT here — those exception categories are
handled by the existing MailFilterRule/MailFilterCategory system (route a letter
out of the milling queue), see tests/test_mail_filters.py."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.routers import settings as settings_router_mod
from app.db import Base
from app.mail_parser import guess_fields_from_text
from app.material_catalog import ensure_seeded as ensure_materials_seeded
from app.material_classifier import seed_alias_rows
from app.models import User
from app.settings_store import get_mail_default_material


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


# ── A: material synonyms flow into the triage guess ─────────────────────────

def test_triage_guess_maps_temporary_crown_to_pmma():
    rows = seed_alias_rows()
    # Realistic call: known_materials is the non-empty sheet vocabulary, so the
    # bare-subject fallback is gated on a fuzzy match (which "врім'янка" fails),
    # letting the classifier backstop resolve it to ПММА.
    guesses = guess_fields_from_text(
        "врім'янка на фрезерування",
        subject="врім'янка",
        body="на фрезерування, дякую",
        known_materials=["циркон а2", "моно а3"],
        material_alias_rows=rows,
    )
    assert guesses["material_color_guess"] == "ПММА"


def test_triage_guess_without_rows_unchanged():
    # No material_alias_rows → the new backstop never runs (back-compat): an
    # unrecognised word stays blank for the operator.
    guesses = guess_fields_from_text(
        "врім'янка",
        subject="врім'янка",
        body="",
        known_materials=["циркон а2", "моно а3"],
    )
    assert guesses["material_color_guess"] is None


def test_triage_guess_explicit_color_wins_over_classifier():
    rows = seed_alias_rows()
    guesses = guess_fields_from_text(
        "колір: моно а3",
        subject="колір: моно а3",
        body="",
        material_alias_rows=rows,
    )
    # The explicit "колір:" capture wins; classifier backstop doesn't overwrite.
    assert guesses["material_color_guess"] == "моно а3"


# ── C: default-material fallback (the mail_reader rule, tested in isolation) ──

def _apply_default(guess_value, service_type, default_material):
    """Mirror the mail_reader rule so its logic is unit-covered without IMAP."""
    guesses = {"material_color_guess": guess_value}
    if default_material and not guesses.get("material_color_guess") and service_type != "3d_print":
        guesses["material_color_guess"] = default_material
    return guesses["material_color_guess"]


def test_default_material_fills_only_empty_milling():
    assert _apply_default(None, None, "Цирконій") == "Цирконій"
    # a real guess is never overwritten
    assert _apply_default("моно а3", None, "Цирконій") == "моно а3"
    # 3D-print letters are not milled here → no default
    assert _apply_default(None, "3d_print", "Цирконій") is None
    # rule off
    assert _apply_default(None, None, None) is None


# ── C routes: default material set/clear/validate + gating ──────────────────

def test_recognition_default_material_set_and_clear():
    with _db() as db:
        admin = _admin(db)
        ensure_materials_seeded(db)
        settings_router_mod.set_recognition_default_material(_request(admin.id), material_name="Цирконій", db=db)
        assert get_mail_default_material(db) == "Цирконій"
        settings_router_mod.set_recognition_default_material(_request(admin.id), material_name="", db=db)
        assert get_mail_default_material(db) is None


def test_recognition_default_material_rejects_unknown():
    with _db() as db:
        admin = _admin(db)
        ensure_materials_seeded(db)
        req = _request(admin.id)
        settings_router_mod.set_recognition_default_material(req, material_name="Неонове скло", db=db)
        assert req.session["recognition_flash"]["kind"] == "error"
        assert get_mail_default_material(db) is None


def test_recognition_operator_forbidden():
    with _db() as db:
        op = _operator(db)
        ensure_materials_seeded(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.set_recognition_default_material(_request(op.id), material_name="Цирконій", db=db)
        assert exc.value.status_code == 403


def test_recognition_non_loopback_forbidden():
    with _db() as db:
        admin = _admin(db)
        ensure_materials_seeded(db)
        with pytest.raises(HTTPException) as exc:
            settings_router_mod.set_recognition_default_material(
                _request(admin.id, host="10.0.0.5"), material_name="Цирконій", db=db
            )
        assert exc.value.status_code == 403
