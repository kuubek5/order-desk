"""Гейт розділів «в розробці / тестується».

Тримає: дефолт із реєстру закриває розділ без міграції, адмін НІКОЛИ не
впирається в блокатор, невідомий стан не пролазить, і таргетинг за ролями
ловить лише вказані ролі.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import User
from app.services import section_gate as sg


def _db() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _user(db, role, name=None):
    u = User(username=name or role, password_hash="x", full_name=role, role=role)
    db.add(u)
    db.commit()
    return u


def test_default_from_registry_blocks_operator_but_not_admin():
    db = _db()
    op, adm = _user(db, "оператор"), _user(db, "адмін")
    assert sg.section_state(db, "stats") == "gauge"
    assert sg.blocked_for(db, op, "stats") == "gauge"
    assert sg.blocked_for(db, adm, "stats") is None
    banner = sg.admin_banner(db, adm, "stats")
    assert banner and banner["state"] == "gauge" and banner["path"] == "/stats"
    assert sg.admin_banner(db, op, "stats") is None


def test_open_state_lets_everyone_in_and_hides_banner():
    db = _db()
    op, adm = _user(db, "оператор"), _user(db, "адмін")
    sg.set_section_state(db, "stats", sg.OPEN)
    db.commit()
    assert sg.section_state(db, "stats") == sg.OPEN
    assert sg.blocked_for(db, op, "stats") is None
    assert sg.admin_banner(db, adm, "stats") is None


def test_variant_switch_persists():
    db = _db()
    sg.set_section_state(db, "stats", "shutter")
    db.commit()
    assert sg.section_state(db, "stats") == "shutter"


def test_unknown_state_or_section_rejected():
    db = _db()
    with pytest.raises(ValueError):
        sg.set_section_state(db, "stats", "banana")
    with pytest.raises(KeyError):
        sg.set_section_state(db, "nope", sg.OPEN)


def test_audience_defaults_to_all():
    db = _db()
    _user(db, "оператор")
    assert sg.section_audience(db, "stats") == sg.AUDIENCE_ALL


def test_audience_by_role_blocks_only_listed_roles():
    db = _db()
    op = _user(db, "оператор")
    tech = _user(db, "технік")  # майбутня роль — має підтягнутись у список
    adm = _user(db, "адмін")
    assert set(sg.non_admin_roles(db)) == {"оператор", "технік"}
    sg.set_section_audience(db, "stats", ["технік"])
    db.commit()
    # закрито лише для техніка; оператор і адмін проходять
    assert sg.blocked_for(db, tech, "stats") == "gauge"
    assert sg.blocked_for(db, op, "stats") is None
    assert sg.blocked_for(db, adm, "stats") is None


def test_audience_admin_role_never_stored_and_empty_is_all():
    db = _db()
    sg.set_section_audience(db, "stats", ["адмін"])  # адміна не закриваємо
    db.commit()
    assert sg.section_audience(db, "stats") == sg.AUDIENCE_ALL
    sg.set_section_audience(db, "stats", [])
    db.commit()
    assert sg.section_audience(db, "stats") == sg.AUDIENCE_ALL


def test_every_variant_has_art_and_copy():
    from pathlib import Path

    arts = Path("app/static/img/blockers")
    for key, copy in sg.VARIANTS.items():
        assert (arts / f"{key}.jpg").is_file(), key
        assert copy["chip"] and copy["title"] and copy["sub"], key
