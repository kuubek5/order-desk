"""Налаштування → Верстати як картки (04.09.26): портрет за дефолтом, живий
кадр — на акаунті.

Що ламається тихо:
- форми: інпути тримаються за form="machine-{id}", окрема форма видалення —
  зламати прив'язку означає «Зберегти» шле порожню форму;
- портрет за моделлю в назві («250» → 250i, решта 350i);
- режим «Живий кадр» бере справжній кадр лише коли він є, інакше чесне
  «чекаємо перший кадр»; вимкнений — «на ремонті»;
- файли портретів, на які посилається CSS, мусять існувати.
"""
import asyncio
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import Headers

from app.routers import auth as auth_router_mod
from app.routers.deps import templates
from app.services.machines import MachineCard, MachineState, MachineTarget

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 4, 12, 0, 0)


def _request(card_mode=""):
    state = SimpleNamespace(ui_prefs_cache={"machine_art": "", "machine_strip": "", "machine_card": card_mode})
    return SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"), headers=Headers({}), state=state)


def _machine(id, name, host, port=8765, enabled=True, token=True):
    return SimpleNamespace(id=id, name=name, host=host, port=port, enabled=enabled,
                           collect_calibration=False,
                           agent_token_encrypted="x" if token else None, password_encrypted=None)


def _card(name, host, percent=None, frame=True, error=None):
    t = MachineTarget(name=name, host=host, port=8765)
    return MachineCard(target=t, state=MachineState(target=t, frame_at=NOW if frame else None,
                                                    percent=percent, percent_at=NOW, error=error), now=NOW)


MACHINES = [
    _machine(1, "350i Loader", "10.0.0.1"),
    _machine(2, "250i dry", "10.0.0.2", port=5900, token=False),
    _machine(3, "350i №2", "10.0.0.3", enabled=False),
]
STATES = {
    1: _card("350i Loader", "10.0.0.1", percent=43),
    2: _card("250i dry", "10.0.0.2", frame=False),
    3: None,
}


def _render(card_mode="", machines=MACHINES, states=STATES):
    tpl = templates.env.get_template("_settings_machines.html")
    return tpl.render(request=_request(card_mode), user=SimpleNamespace(role="адмін"),
                      machines=machines, machine_state_by_id=states, machine_password_set=True)


def test_cards_keep_forms_wired_and_pick_portrait_by_model():
    html = _render()
    assert "fu-table" not in html
    assert html.count('class="mset-card') == 4          # три верстати + «Додати»
    # кожен інпут рядка тримається за свою форму, форма видалення окрема
    for mid in (1, 2, 3):
        assert f'id="machine-{mid}"' in html and f'id="machine-del-{mid}"' in html
        assert html.count(f'form="machine-{mid}"') >= 7   # name host port token password + 2 перемикачі
    assert 'data-model="350i"' in html and 'data-model="250i"' in html
    assert html.count('data-model="250i"') == 1
    assert "фрезерує · 43%" in html
    assert "чекаємо кадр" in html
    assert "на ремонті" in html
    assert 'HTTP-агент · 8765' in html and 'VNC · 5900' in html
    assert 'action="/settings/machines/password"' in html


def test_frame_mode_uses_real_frame_only_when_present():
    html = _render("frame")
    assert 'class="mset mode-frame"' in html
    assert "mset-port" not in html.replace('mset-port add', '')   # портретів немає, лише картка «Додати»
    assert "/machines/10.0.0.1-8765/frame.png?t=" in html
    assert "/machines/10.0.0.2" not in html                      # без кадру — без картинки
    assert "чекаємо перший кадр" in html
    assert "на ремонті · опитування зупинено" in html
    assert 'class="mset-pct mono">43<u>%</u>' in html


def test_default_mode_has_no_frame_markup():
    html = _render("")
    assert "mset-shot" not in html and "mode-frame" not in html


def test_appearance_accepts_card_pref_and_rejects_junk():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import User

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        u = User(username="op", password_hash="x", full_name="Оп", role="оператор")
        db.add(u); db.commit()
        req = SimpleNamespace(session={"user_id": u.id}, client=SimpleNamespace(host="127.0.0.1"),
                              headers=Headers({}), state=SimpleNamespace())
        base = dict(theme="", icons="", buttons="", loader="", chips="", machine_art="", machine_strip="")
        r = asyncio.run(auth_router_mod.post_account_appearance(request=req, db=db, machine_card="frame", **base))
        assert r.status_code == 204
        db.refresh(u); assert u.ui_machine_card == "frame"
        r = asyncio.run(auth_router_mod.post_account_appearance(request=req, db=db, machine_card="polaroid", **base))
        assert r.status_code == 422


def test_portrait_files_referenced_by_css_exist():
    css = (ROOT / "app/static/css/settings.css").read_text(encoding="utf-8")
    names = set(re.findall(r'img/(machine-portrait-[a-z0-9]+\.jpg)', css))
    assert names == {"machine-portrait-350i.jpg", "machine-portrait-250i.jpg"}
    for n in names:
        assert (ROOT / "app/static/img" / n).is_file(), n
