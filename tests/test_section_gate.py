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


# ── Реєстр на всі екрани (10.09.26) ─────────────────────────────────────────
# Прохання власника: адмін має вміти зачинити БУДЬ-ЯКУ сторінку для решти.
# Механізм був готовий, але в реєстрі стояв один розділ (`stats`).


def test_new_sections_default_to_open():
    """Дефолт — це стан на щойно оновленому застосунку, де адмін ще нічого не
    чіпав. Арт у дефолті означав би, що оновлення САМО зачинило екран усім
    операторам, нікого не спитавши. `stats` лишається винятком історично."""
    db = _db()
    closed_by_default = [
        key for key in sg.SECTIONS if sg.section_state(db, key) != sg.OPEN
    ]
    assert closed_by_default == ["stats"]


def test_registry_covers_every_operator_screen():
    """Знімок складу: розділ додають/прибирають СВІДОМО, як у route_inventory.

    `/journal/sync` і `/feedback/inbox` сюди не входять — вони й так лише для
    адміна; `/account` теж, бо це власний кабінет (зачинивши його, людину
    позбавили б способу змінити свій пароль)."""
    assert set(sg.SECTIONS) == {
        "queue", "mail", "handout", "shift", "furnaces", "machines", "discs",
        "clients", "archive", "journal", "stats", "vyrobitok", "settings",
    }
    for key, meta in sg.SECTIONS.items():
        assert meta["path"].startswith("/"), key
        assert meta["title"], key
        assert meta["default"] == sg.OPEN or meta["default"] in sg.VARIANTS, key


def test_every_section_path_is_unique():
    paths = [meta["path"] for meta in sg.SECTIONS.values()]
    assert len(paths) == len(set(paths))


def test_closed_sections_lists_only_the_closed_ones():
    db = _db()
    assert list(sg.closed_sections(db)) == ["stats"]      # дефолт реєстру
    sg.set_section_state(db, "furnaces", "shutter")
    sg.set_section_state(db, "stats", sg.OPEN)
    assert sg.closed_sections(db) == {"furnaces": "shutter"}


def test_admin_is_never_blocked_on_any_section():
    db = _db()
    adm = _user(db, "адмін")
    for key in sg.SECTIONS:
        sg.set_section_state(db, key, "shutter")
        assert sg.blocked_for(db, adm, key) is None, key


def test_closing_every_section_blocks_the_operator_everywhere():
    db = _db()
    op = _user(db, "оператор")
    for key in sg.SECTIONS:
        sg.set_section_state(db, key, "shutter")
        assert sg.blocked_for(db, op, key) == "shutter", key


def test_section_keys_pass_the_settings_guard():
    """Ключі гейта пізнаються за префіксом (їх 24 на дванадцять розділів), але
    чужий ключ має лишатись відхиленим."""
    from app.settings_store import set_setting

    db = _db()
    for key in sg.SECTIONS:
        sg.set_section_state(db, key, "mill")          # не кидає
        sg.set_section_audience(db, key, ["оператор"])
    with pytest.raises(ValueError):
        set_setting(db, "section_not_a_real_prefix:queue", "mill")


def test_slab_counts_closed_sections():
    """Плита рахувала закриті через `getattr` по СЛОВНИКУ, тобто завжди нуль:
    «усі відкриті» стояло й тоді, коли розділ був зачинений (10.09.26)."""
    from app.services.settings_status import _slab_sections

    db = _db()
    sg.set_section_state(db, "furnaces", "shutter")
    slab = _slab_sections({"sections_admin": sg.sections_admin(db)})
    assert "закрито" in slab.label
    closed_meter = next(m for m in slab.meters if m.k == "Закрито")
    assert closed_meter.v == "2"          # furnaces + stats (дефолт реєстру)

    sg.set_section_state(db, "furnaces", sg.OPEN)
    sg.set_section_state(db, "stats", sg.OPEN)
    assert _slab_sections({"sections_admin": sg.sections_admin(db)}).label == "усі відкриті"
