"""Віджети шапки черги (Верстати / Sisma) вимикаються в шестерні (10.09.26).

Що ламається тихо:
- віджет мусить слухатись налаштування і на ПОЛЛІ, а не лише на першому
  рендері сторінки — інакше сховане поверталось би за 10–15 с;
- обгортка з поллом лишається й у схованому віджеті — інакше увімкнути назад
  без перезавантаження сторінки не вийшло б;
- на акаунт потрапляють лише відомі ключі, а порожньо = усе видно.
"""
import asyncio
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from app.db import Base
from app.models import User
from app.routers import auth as auth_router
from app.routers.deps import templates
from app.services.machines import MachineCard, MachineState, MachineTarget
from app.services.widget_order import HIDEABLE_WIDGETS, clean_hidden_widgets, hidden_widgets_set
from tests.test_settings_slabs_render import app_db  # noqa: F401 — фікстура


def _request(hidden: str = ""):
    state = SimpleNamespace(ui_prefs_cache={
        "hidden_widgets": hidden, "load_metrics": "crm,pc,ram", "side_order": "",
        "strip_order": "", "machine_card": "", "machine_art": "", "machine_strip": ""})
    return SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"),
                           headers=Headers({}), state=state)


@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    (None, ""),
    ("sisma", "sisma"),
    ("sisma,machines", "machines,sisma"),   # сталий порядок
    ("machines,ghost", "machines"),         # чуже відсіюється
    ("side-sync,side-mail", "side-mail,side-sync"),
    ("machine,mail", ""),                   # голий ключ секції — не наш
])
def test_clean_hidden_widgets(raw, expected):
    assert clean_hidden_widgets(raw) == expected


def test_hidden_widgets_set_empty_means_all_shown():
    assert hidden_widgets_set("") == set()
    assert hidden_widgets_set("machines") == {"machines"}


def _machine_cards():
    now = datetime(2026, 9, 10, 12, 0)
    target = MachineTarget(name="350i L", host="10.0.0.1")
    state = MachineState(target=target, frame_at=now, percent=43, percent_at=now)
    return [MachineCard(target=target, state=state, now=now)]


def _strip(hidden):
    return templates.env.get_template("_machine_strip.html").render(
        request=_request(hidden), machine_cards=_machine_cards(),
        machine_summary={"total": 1, "running": 1, "broken": 0})


def test_machine_strip_hides_but_keeps_its_poll():
    assert "350i L" in _strip("")
    off = _strip("machines")
    assert "350i L" not in off
    assert 'id="machine-strip"' in off and 'hx-get="/machines/strip"' in off and "hidden" in off
    assert "350i L" in _strip("sisma"), "сховано Sisma — верстати мусять лишитись"


def _printer():
    return SimpleNamespace(
        target=SimpleNamespace(name="Sisma MYSINT", host="10.0.0.9"),
        has_problem=False, is_completed=False, layers=(120, 400), percent=30,
        lasing=True, ends_at=datetime(2026, 9, 10, 18, 40), started_at=None,
        left_text="", frame_at=None, problem_text="",
    )


def _sisma(hidden):
    return templates.env.get_template("_sisma_widget.html").render(
        request=_request(hidden), sisma_cards=[_printer()])


def test_sisma_widget_hides_but_keeps_its_poll():
    assert "Sisma MYSINT" in _sisma("")
    off = _sisma("sisma")
    assert "Sisma MYSINT" not in off
    assert 'id="sisma-strip"' in off and 'hx-get="/machines/sisma"' in off and "hidden" in off
    assert "Sisma MYSINT" in _sisma("machines"), "сховано верстати — Sisma мусить лишитись"


def test_gear_row_reflects_account():
    tpl = templates.env.from_string(
        '{% from "_lookgear.html" import lookgear %}'
        "{{ lookgear('queue', 'body', 0, 2, col_edit=true, widgets_hidden=hidden) }}"
    )
    html = tpl.render(hidden={"sisma", "side-sync"})
    pressed = dict(re.findall(r'data-header-widget="([\w-]+)"\s+aria-pressed="(\w+)"', html))
    assert pressed["machines"] == "true" and pressed["sisma"] == "false"
    assert pressed["side-sync"] == "false" and pressed["side-mail"] == "true"
    # Сторож: кнопки в шестерні = ключі, які сервер приймає. Розійдуться — і
    # кнопка мовчки нічого не зберігатиме (сервер відсіє невідомий ключ).
    assert set(pressed) == set(HIDEABLE_WIDGETS)
    # Тріаж пошти цього рядка не має: там шапка інша.
    mail = templates.env.from_string(
        '{% from "_lookgear.html" import lookgear %}{{ lookgear("mail", "body", 0, 2) }}'
    ).render()
    assert "data-header-widget" not in mail


def test_poll_obeys_the_switch_end_to_end(app_db, monkeypatch):  # noqa: F811
    """Справжній застосунок: зберегли «сховати» → полл віджета віддає порожню
    обгортку; увімкнули → віджет повертається на наступному ж поллі."""
    from app.routers import machines as machines_router
    from tests.asgi_client import MiniClient
    from tests.test_settings_slabs_render import ADMIN

    app, _ = app_db
    monkeypatch.setattr(machines_router, "machine_side_context", lambda db: {
        "machine_cards": _machine_cards(), "machine_summary": {"total": 1, "running": 1, "broken": 0}})
    monkeypatch.setattr(machines_router, "sisma_context", lambda db: {"sisma_cards": [_printer()]})
    client = MiniClient(app)
    client.login(*ADMIN)

    assert "350i L" in client.get("/machines/strip")[2], "підміна не влучила — тест був би порожнім"
    assert "Sisma MYSINT" in client.get("/machines/sisma")[2]

    assert client.post("/account/header-widgets", {"hidden": "machines,sisma"})[0] == 204
    strip, sisma = client.get("/machines/strip")[2], client.get("/machines/sisma")[2]
    assert "350i L" not in strip and 'id="machine-strip"' in strip
    assert "Sisma MYSINT" not in sisma and 'id="sisma-strip"' in sisma

    client.post("/account/header-widgets", {"hidden": ""})
    assert "350i L" in client.get("/machines/strip")[2]
    assert "Sisma MYSINT" in client.get("/machines/sisma")[2]


#: Атрибут `hidden` окремим словом — не «document.hidden» у hx-trigger полла.
_HIDDEN_ATTR = re.compile(r"\shidden(?=[\s>])")


def _side_state(html):
    """{секція: схована?} і чи схована вся панель — з відрендереної черги."""
    secs = {m.group(1): bool(_HIDDEN_ATTR.search(m.group(0)))
            for m in re.finditer(r'<section class="side-sec[^"]*"[^>]*?data-sec="(\w+)"[^>]*>', html)}
    aside = re.search(r'<aside class="side-panel"[^>]*>', html).group(0)
    return secs, bool(_HIDDEN_ATTR.search(aside))


def test_side_sections_hide_and_the_panel_goes_when_all_are_hidden(app_db):  # noqa: F811
    from tests.asgi_client import MiniClient
    from tests.test_settings_slabs_render import ADMIN

    app, _ = app_db
    client = MiniClient(app)
    client.login(*ADMIN)

    secs, panel_hidden = _side_state(client.get("/")[2])
    assert set(secs) == {"mail", "furnace", "machine", "sync", "handout"}
    assert not any(secs.values()) and not panel_hidden, "за замовчуванням видно все"

    client.post("/account/header-widgets", {"hidden": "side-mail,side-furnace"})
    secs, panel_hidden = _side_state(client.get("/")[2])
    assert secs == {"mail": True, "furnace": True, "machine": False, "sync": False, "handout": False}
    assert not panel_hidden
    # Секція з власним поллом слухається тумблера і на поллі.
    furnace = client.get("/furnaces/side")[2]
    furnace_tag = re.search(r'<section[^>]*id="furnace-side"[^>]*>', furnace).group(0)
    assert _HIDDEN_ATTR.search(furnace_tag)
    machine = client.get("/machines/side")[2]
    assert not _HIDDEN_ATTR.search(re.search(r'<section[^>]*id="machine-side"[^>]*>', machine).group(0))

    client.post("/account/header-widgets",
                {"hidden": "side-mail,side-furnace,side-machine,side-sync,side-handout"})
    _, panel_hidden = _side_state(client.get("/")[2])
    assert panel_hidden, "сховано все — панель мусить зникнути, а таблиця взяти ширину"

    client.post("/account/header-widgets", {"hidden": ""})
    secs, panel_hidden = _side_state(client.get("/")[2])
    assert not any(secs.values()) and not panel_hidden


def test_route_saves_filters_and_needs_login():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
        db.add(user)
        db.commit()
        assert user.queue_hidden_widgets == "", "новий акаунт бачить усе"
        req = SimpleNamespace(session={"user_id": user.id}, client=SimpleNamespace(host="127.0.0.1"))

        r = asyncio.run(auth_router.post_account_header_widgets(request=req, hidden="sisma,ghost", db=db))
        assert r.status_code == 204
        db.refresh(user)
        assert user.queue_hidden_widgets == "sisma"

        asyncio.run(auth_router.post_account_header_widgets(request=req, hidden="", db=db))
        db.refresh(user)
        assert user.queue_hidden_widgets == ""

        anon = SimpleNamespace(session={}, client=SimpleNamespace(host="127.0.0.1"))
        r = asyncio.run(auth_router.post_account_header_widgets(request=anon, hidden="machines", db=db))
        assert r.status_code == 401
