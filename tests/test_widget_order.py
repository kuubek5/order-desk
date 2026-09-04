"""Порядок віджетів черги: секції правої панелі й смуга верстатів (04.09.26).

Що ламається тихо:
- порядок мусить малювати СЕРВЕР (`style="order:N"`, сортування карток),
  інакше він помре на першому ж поллі — смуга свапається кожні 10 с;
- збережений список — побажання: зникла секція чи верстат не мають ламати
  решту, нова секція не має стрибати на початок;
- роут зберігає СВОЄМУ користувачеві й відсіює чуже, а не відхиляє все.
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from app.db import Base
from app.models import User
from app.routers import auth as auth_router_mod
from app.routers.deps import templates
from app.services.widget_order import (
    SIDE_SECTIONS,
    clean_side_order,
    clean_strip_order,
    side_index,
    sort_machine_cards,
)


def test_side_index_keeps_default_tail_after_saved_ones():
    saved = "machine,mail"
    assert side_index(saved, "machine") == 0
    assert side_index(saved, "mail") == 1
    # Незбережені йдуть далі В ПОРЯДКУ ЗА ЗАМОВЧУВАННЯМ, а не абияк.
    rest = [s for s in SIDE_SECTIONS if s not in ("machine", "mail")]
    assert [side_index(saved, s) for s in rest] == [2, 3, 4]


def test_side_index_without_saved_order_is_the_default_order():
    assert [side_index("", s) for s in SIDE_SECTIONS] == list(range(len(SIDE_SECTIONS)))
    assert [side_index(None, s) for s in SIDE_SECTIONS] == list(range(len(SIDE_SECTIONS)))


def test_side_index_ignores_unknown_keys_in_saved_value():
    # Секцію прибрали з розмітки — решта порядку не має поїхати.
    assert side_index("ghost,machine", "machine") == 0


@pytest.mark.parametrize("raw,expected", [
    ("machine,mail", "machine,mail"),
    ("machine,ghost,mail", "machine,mail"),
    (" machine , machine ,mail", "machine,mail"),
    ("", ""),
    (None, ""),
    ("<script>", ""),
])
def test_clean_side_order(raw, expected):
    assert clean_side_order(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("3,1,2", "3,1,2"),
    ("3,abc,1", "3,1"),
    ("0,2", "2"),
    ("-1,2", "2"),
    ("", ""),
])
def test_clean_strip_order(raw, expected):
    assert clean_strip_order(raw) == expected


def _card(machine_id):
    return SimpleNamespace(target=SimpleNamespace(machine_id=machine_id))


def test_sort_machine_cards_puts_unknown_last_keeping_their_order():
    cards = [_card(1), _card(2), _card(3), _card(4)]
    ordered = sort_machine_cards("3,1", cards)
    assert [c.target.machine_id for c in ordered] == [3, 1, 2, 4]


def test_sort_machine_cards_without_saved_order_is_untouched():
    cards = [_card(1), _card(2)]
    assert sort_machine_cards("", cards) == cards
    assert sort_machine_cards(None, cards) == cards


def test_sort_machine_cards_survives_card_without_row():
    cards = [_card(None), _card(2)]
    ordered = sort_machine_cards("2", cards)
    assert [c.target.machine_id for c in ordered] == [2, None]


def _request(side_order="", strip_order=""):
    prefs = {"machine_card": "", "machine_art": "", "machine_strip": "",
             "side_order": side_order, "strip_order": strip_order}
    return SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"),
                           headers=Headers({}), state=SimpleNamespace(ui_prefs_cache=prefs))


def test_side_partials_render_server_side_order():
    furnace = templates.env.get_template("_furnace_side.html").render(
        request=_request("machine,furnace"), furnace_cards=[],
        furnace_summary={"total": 0, "running": 0, "broken": 0},
        furnaces_configured=0, furnaces_all_idle=False)
    assert 'style="order:1"' in furnace
    machine = templates.env.get_template("_machine_side.html").render(
        request=_request("machine,furnace"), machine_cards=[],
        machine_summary={"total": 0, "running": 0, "broken": 0})
    assert 'style="order:0"' in machine


def test_strip_renders_saved_order_and_machine_ids():
    from datetime import datetime
    from app.services.machines import MachineCard, MachineState, MachineTarget

    now = datetime(2026, 9, 4, 12, 0, 0)

    def card(name, host, mid, percent):
        target = MachineTarget(name=name, host=host, machine_id=mid)
        return MachineCard(target=target, state=MachineState(target=target, frame_at=now,
                                                             percent=percent, percent_at=now), now=now)

    cards = [card("A", "10.0.0.1", 1, 10), card("B", "10.0.0.2", 2, 20), card("C", "10.0.0.3", 3, 30)]
    html = templates.env.get_template("_machine_strip.html").render(
        request=_request(strip_order="3,1"), machine_cards=cards,
        machine_summary={"total": 3, "running": 3, "broken": 0})
    assert html.index('data-mid="3"') < html.index('data-mid="1"') < html.index('data-mid="2"')


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def test_layout_route_saves_per_user_and_filters_junk():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
        other = User(username="op2", password_hash="x", full_name="Оп2", role="оператор")
        db.add_all([user, other])
        db.commit()
        req = SimpleNamespace(session={"user_id": user.id}, client=SimpleNamespace(host="127.0.0.1"))

        assert asyncio.run(auth_router_mod.post_account_layout(
            request=req, scope="side", order="machine,ghost,mail", db=db)).status_code == 204
        assert asyncio.run(auth_router_mod.post_account_layout(
            request=req, scope="strip", order="3,x,1", db=db)).status_code == 204
        db.refresh(user)
        db.refresh(other)
        assert user.queue_side_order == "machine,mail"
        assert user.queue_strip_order == "3,1"
        # чужий акаунт не зачеплено
        assert other.queue_side_order == "" and other.queue_strip_order == ""

        assert asyncio.run(auth_router_mod.post_account_layout(
            request=req, scope="hack", order="1", db=db)).status_code == 422
        anon = SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"))
        assert asyncio.run(auth_router_mod.post_account_layout(
            request=anon, scope="side", order="mail", db=db)).status_code == 401


def test_lookgear_has_the_widget_edit_button():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    gear = (root / "app/templates/_lookgear.html").read_text(encoding="utf-8")
    assert "data-widget-edit-toggle" in gear
    base = (root / "app/templates/base.html").read_text(encoding="utf-8")
    assert "js/widgetedit.js" in base and "widgetEditMode" in base
