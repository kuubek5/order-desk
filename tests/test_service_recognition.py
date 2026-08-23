"""Mail recognition dictionaries (A/B/C): the editable service-type keyword
catalog, the default-material fallback, and their wiring into the triage
parser, plus the /settings/recognition routes. Route handlers are called
directly with a fake request, same style as tests/test_material_settings_routes."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.mail_parser import guess_fields_from_text, guess_service_type
from app.material_catalog import ensure_seeded as ensure_materials_seeded, load_alias_rows
from app.material_classifier import seed_alias_rows
from app.models import ServiceKeyword, User
from app.service_catalog import (
    ServiceCatalogError,
    add_keyword,
    ensure_seeded,
    list_keywords,
    load_service_rows,
)
from app.service_classifier import classify_service, seed_service_rows
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


# ── B: pure service classifier ──────────────────────────────────────────────

def test_classify_service_seed_flags_3d():
    assert classify_service("Треба 3d друк моделі") == "3d_print"
    assert classify_service("надрукувати каппу") == "3d_print"
    assert classify_service("resin print please") == "3d_print"


def test_classify_service_milling_is_none():
    assert classify_service("фрезерування циркон а2") is None
    assert classify_service("") is None
    assert classify_service(None) is None


def test_classify_service_token_not_substring():
    # "sla" is a token rule — must not fire inside an unrelated word.
    rows = seed_service_rows()
    assert classify_service("надіслати файл", rows) is None
    assert classify_service("друк на sla принтері", rows) == "3d_print"


# ── B: guess_service_type honours editable rows ─────────────────────────────

def test_guess_service_type_uses_rows_over_regex():
    # A custom keyword not in the hardcoded regex still flags when rows drive it.
    from app.service_classifier import ServiceKeywordRow

    rows = [ServiceKeywordRow(pattern="фотополімер", match_type="contains", service_type="3d_print")]
    assert guess_service_type("виріб з фотополімеру", rows) == "3d_print"
    # And a hardcoded-regex word does NOT flag when the editable set omits it.
    assert guess_service_type("3d друк моделі", rows) is None


def test_guess_service_type_regex_fallback_when_rows_none():
    # Back-compat: no rows → the original regex seed still works.
    assert guess_service_type("3d друк") == "3d_print"
    assert guess_service_type("звичайне фрезерування") is None


# ── B: DB catalog CRUD ──────────────────────────────────────────────────────

def test_ensure_seeded_populates_and_is_idempotent():
    with _db() as db:
        ensure_seeded(db)
        first = len(list_keywords(db))
        assert first > 0
        ensure_seeded(db)
        assert len(list_keywords(db)) == first


def test_add_keyword_normalizes_and_rejects_duplicate():
    with _db() as db:
        ensure_seeded(db)
        add_keyword(db, "  ФотоПолімер  ", "contains")
        stored = db.scalar(select(ServiceKeyword).where(ServiceKeyword.pattern == "фотополімер"))
        assert stored is not None
        with pytest.raises(ServiceCatalogError):
            add_keyword(db, "фотополімер", "contains")


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


# ── UI routes: gating + mutations ───────────────────────────────────────────

def test_recognition_add_keyword_flashes_and_persists():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        req = _request(admin.id)
        resp = web.add_service_keyword(req, pattern="фотополімер", match_type="contains", db=db)
        assert resp.status_code == 303
        assert req.session["recognition_flash"]["kind"] == "success"
        assert "3d_print" == classify_service("виріб з фотополімеру", load_service_rows(db))


def test_recognition_delete_keyword():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        kw = db.scalar(select(ServiceKeyword))
        web.remove_service_keyword(kw.id, _request(admin.id), db=db)
        assert db.get(ServiceKeyword, kw.id) is None


def test_recognition_default_material_set_and_clear():
    with _db() as db:
        admin = _admin(db)
        ensure_materials_seeded(db)
        web.set_recognition_default_material(_request(admin.id), material_name="Цирконій", db=db)
        assert get_mail_default_material(db) == "Цирконій"
        web.set_recognition_default_material(_request(admin.id), material_name="", db=db)
        assert get_mail_default_material(db) is None


def test_recognition_default_material_rejects_unknown():
    with _db() as db:
        admin = _admin(db)
        ensure_materials_seeded(db)
        req = _request(admin.id)
        web.set_recognition_default_material(req, material_name="Неонове скло", db=db)
        assert req.session["recognition_flash"]["kind"] == "error"
        assert get_mail_default_material(db) is None


def test_recognition_operator_forbidden():
    with _db() as db:
        op = _operator(db)
        ensure_seeded(db)
        with pytest.raises(HTTPException) as exc:
            web.add_service_keyword(_request(op.id), pattern="x", match_type="contains", db=db)
        assert exc.value.status_code == 403


def test_recognition_non_loopback_forbidden():
    with _db() as db:
        admin = _admin(db)
        ensure_seeded(db)
        with pytest.raises(HTTPException) as exc:
            web.add_service_keyword(
                _request(admin.id, host="10.0.0.5"), pattern="x", match_type="contains", db=db
            )
        assert exc.value.status_code == 403
