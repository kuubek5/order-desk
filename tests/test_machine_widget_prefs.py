"""Віджет верстатів: арт секції та стрічка «назва + %» на акаунті (03.09.26).

Що ламається тихо:
- стрічка мусить віддавати обгортку ЗАВЖДИ (без верстатів, з вимкненою
  стрічкою) — інакше елемент зникне з DOM разом зі своїм поллом;
- варіант з акаунта мусить долітати до розмітки (segments рендерить десять
  сегментів, ring — коло, ticker — без чіпів);
- невалідне значення з форми полетіло б атрибутом у <html>;
- CSS посилається на файли артів — файл, якого немає, дав би голу картку без
  жодної помилки.
"""
import asyncio
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from app.db import Base
from app.models import User
from app.routers import auth as auth_router_mod
from app.routers.deps import templates
from app.services.machines import MachineCard, MachineState, MachineTarget

ROOT = Path(__file__).resolve().parents[1]


def _request(prefs: dict | None = None):
    state = SimpleNamespace()
    if prefs is not None:
        base = {"machine_art": "", "machine_strip": ""}
        base.update(prefs)
        state.ui_prefs_cache = base
    return SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"),
                           headers=Headers({}), state=state)


def _cards():
    now = datetime(2026, 9, 3, 12, 0, 0)

    def card(name, host, percent=None, error=None, frame=True):
        target = MachineTarget(name=name, host=host)
        state = MachineState(target=target, frame_at=now if frame else None,
                             percent=percent, percent_at=now, error=error)
        return MachineCard(target=target, state=state, now=now)

    return [
        card("350i L", "10.0.0.1", percent=43),
        card("250i", "10.0.0.2"),                       # стоїть
        card("450i", "10.0.0.3", error="немає зв'язку"),
        card("650i", "10.0.0.4", frame=False),          # чекаємо кадр
    ]


def _render(prefs=None, cards=None):
    tpl = templates.env.get_template("_machine_strip.html")
    cards = _cards() if cards is None else cards
    return tpl.render(request=_request(prefs), machine_cards=cards,
                      machine_summary={"total": len(cards), "running": 1, "broken": 1})


def test_strip_wrapper_survives_empty_and_off():
    empty = _render(cards=[])
    assert 'id="machine-strip"' in empty and "hidden" in empty
    assert 'hx-get="/machines/strip"' in empty

    off = _render({"machine_strip": "off"})
    assert 'id="machine-strip"' in off and "hidden" in off
    assert "350i L" not in off


def test_strip_default_is_segments_with_ten_segments_per_machine():
    html = _render()
    assert 'data-variant="segments"' in html
    # 43 % → 4 сегменти світяться з 10; «—» без єдиного.
    run = re.search(r'<a class="mch run"[\s\S]*?</a>', html).group(0)
    assert len(re.findall(r'<s( class="on")?></s>', run)) == 10 and run.count('class="on"') == 4
    idle = re.search(r'<a class="mch idle"[\s\S]*?</a>', html).group(0)
    assert idle.count('class="on"') == 0 and "—" in idle
    assert 'class="mch off"' in html and 'class="mch wait"' in html


def test_strip_variants_change_markup():
    ring = _render({"machine_strip": "ring"})
    assert 'data-variant="ring"' in ring and '<svg class="ring"' in ring and "segs" not in ring
    edge = _render({"machine_strip": "edge"})
    assert 'class="mch-edge"' in edge and "segs" not in edge
    ticker = _render({"machine_strip": "ticker"})
    assert 'class="mstrip-ticker"' in ticker and 'class="mch' not in ticker
    assert "350i L" in ticker and "43" in ticker


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db):
    u = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(u)
    db.commit()
    return u


def _post(user_id, db, **over):
    kwargs = {"theme": "", "icons": "", "buttons": "", "loader": "", "chips": "",
              "machine_art": "", "machine_strip": ""}
    kwargs.update(over)
    req = SimpleNamespace(session={"user_id": user_id}, client=SimpleNamespace(host="127.0.0.1"),
                          headers=Headers({}), state=SimpleNamespace())
    return asyncio.run(auth_router_mod.post_account_appearance(request=req, db=db, **kwargs))


def test_appearance_saves_and_guards_machine_widget_prefs():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        assert _post(user.id, db, machine_art="burr", machine_strip="ring").status_code == 204
        db.refresh(user)
        assert (user.ui_machine_art, user.ui_machine_strip) == ("burr", "ring")

        assert _post(user.id, db, machine_art="hotdog").status_code == 422
        assert _post(user.id, db, machine_strip="marquee").status_code == 422
        db.refresh(user)
        assert (user.ui_machine_art, user.ui_machine_strip) == ("burr", "ring")

        # Дефолт власника — порожній рядок, none/off — вимкнути.
        assert _post(user.id, db, machine_art="none", machine_strip="off").status_code == 204


def test_every_machine_art_referenced_by_css_exists():
    css = (ROOT / "app/static/css/furnaces.css").read_text(encoding="utf-8")
    css += (ROOT / "app/static/css/settings.css").read_text(encoding="utf-8")
    names = set(re.findall(r'img/(machine-[a-z]+\.jpg)', css))
    assert {"machine-dust.jpg", "machine-burr.jpg", "machine-flower.jpg",
            "machine-titanium.jpg", "machine-toolpath.jpg"} <= names
    for name in names:
        assert (ROOT / "app/static/img" / name).is_file(), name
    # Кожен арт із білого списку акаунта має свій файл — і навпаки.
    for value in auth_router_mod.UI_MACHINE_ARTS - {"", "none"}:
        assert f"machine-{value}.jpg" in names, value


def test_side_tiles_carry_idle_and_wait_states():
    tpl = templates.env.get_template("_machine_side.html")
    cards = _cards()
    html = tpl.render(request=None, machine_cards=cards,
                      machine_summary={"total": 4, "running": 1, "broken": 1})
    assert "ms-tile is-run" in html
    assert "ms-tile is-idle" in html
    assert "ms-tile is-offline" in html
    assert "ms-tile is-wait" in html
