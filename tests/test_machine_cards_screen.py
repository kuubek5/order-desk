"""Екран «Верстати» як портретні картки (04.09.26) + таблиця в налаштуваннях
зі стилями, яких там не було.

Що ламається тихо:
- екран «Верстати»: дефолт — портрет за моделлю в назві (три стовпці);
  «Живий кадр» на акаунті повертає стару розмітку з кадром у плитці; умова
  полла (.fu-frame[open]) мусить лишитись в обох;
- налаштування: таблиця з формами form="machine-{id}" і рядок «Додати» з
  усіма полями В ОДНІЙ формі (власник: «немає змоги додати верстат» — рядок
  був голим, бо furnaces.css на цьому екрані не підключений; стилі тепер у
  settings.css, і тест тримає їх там);
- файли портретів, на які посилається CSS, існують.
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


def _card(name, host, percent=None, frame=True, error=None, port=8765):
    t = MachineTarget(name=name, host=host, port=port)
    return MachineCard(target=t, state=MachineState(target=t, frame_at=NOW if frame else None,
                                                    percent=percent, percent_at=NOW, error=error), now=NOW)


CARDS = [
    _card("350i Loader", "10.0.0.1", percent=43),
    _card("250i dry", "10.0.0.2", frame=False, port=5900),
    _card("350i №2", "10.0.0.3", error="немає зв'язку"),
]


def _render_screen(card_mode=""):
    tpl = templates.env.get_template("_machine_cards.html")
    return tpl.render(request=_request(card_mode), cards=CARDS, user=SimpleNamespace(role="адмін"))


def test_screen_default_is_portrait_grid_by_model():
    html = _render_screen()
    assert 'class="mc-grid"' in html and "fu-grid" not in html
    assert html.count('data-model="350i"') == 2 and html.count('data-model="250i"') == 1
    assert '>43<u>%</u>' in html                       # головне число в шапці
    assert "чекаємо кадр" in html and "немає зв'язку" in html
    # кадр лишається доступним — за details, з тією ж умовою для полла
    assert '<details class="fu-frame">' in html
    assert "/machines/10.0.0.1-8765/frame.png?t=" in html
    assert "/machines/10.0.0.2" not in html             # без кадру — без картинки
    assert "mc-shot" not in html


def test_screen_frame_mode_keeps_old_markup():
    html = _render_screen("frame")
    assert 'class="fu-grid"' in html and "mc-grid" not in html
    assert 'class="mc-shot"' in html and '<details class="fu-frame">' in html
    assert "data-model" not in html


def test_screen_empty_state_survives_both_modes():
    tpl = templates.env.get_template("_machine_cards.html")
    for mode in ("", "frame"):
        html = tpl.render(request=_request(mode), cards=[], user=SimpleNamespace(role="адмін"))
        assert "не налаштовано" in html


def _machine(id, name, host, port=8765, enabled=True, token=True):
    return SimpleNamespace(id=id, name=name, host=host, port=port, enabled=enabled, collect_calibration=False,
                           agent_token_encrypted="x" if token else None, password_encrypted=None)


def test_settings_table_forms_and_add_row_are_wired():
    tpl = templates.env.get_template("_settings_machines.html")
    html = tpl.render(request=_request(), user=SimpleNamespace(role="адмін"),
                      machines=[_machine(1, "350i Loader", "10.0.0.1"), _machine(2, "250i", "10.0.0.2", port=5900, token=False)],
                      machine_password_set=True)
    assert '<table class="fu-table">' in html
    for mid in (1, 2):
        assert f'id="machine-{mid}"' in html and f'id="machine-del-{mid}"' in html
        assert html.count(f'form="machine-{mid}"') >= 7
    # рядок «Додати»: усі поля й кнопка всередині ОДНІЄЇ форми на /settings/machines
    add = re.search(r'<form method="post" action="/settings/machines" class="fu-add">([\s\S]*?)</form>', html)
    assert add, "форма додавання зникла"
    body = add.group(1)
    for field in ('name="name"', 'name="host"', 'name="port"', 'name="agent_token"', 'name="password"'):
        assert field in body, field
    assert 'type="submit"' in body and "Додати" in body


def test_settings_table_styles_live_on_the_settings_screen():
    """furnaces.css на /settings не підключений — правила таблиці мусять бути
    в settings.css, інакше розділ знову стане голим (і «Додати» — стовпчиком)."""
    settings_html = (ROOT / "app/templates/settings.html").read_text(encoding="utf-8")
    assert "furnaces.css" not in settings_html
    css = (ROOT / "app/static/css/settings.css").read_text(encoding="utf-8")
    for rule in (".setv2 .fu-table{", ".setv2 .fu-add-row{", ".setv2 .fu-col-act{"):
        assert rule in css, rule


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
    css = (ROOT / "app/static/css/furnaces.css").read_text(encoding="utf-8")
    names = set(re.findall(r'img/(machine-portrait-[a-z0-9]+\.jpg)', css))
    assert names == {"machine-portrait-350i.jpg", "machine-portrait-250i.jpg"}
    for n in names:
        assert (ROOT / "app/static/img" / n).is_file(), n
