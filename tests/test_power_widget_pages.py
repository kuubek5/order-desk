"""Віджет «Світло» на сторінках: CRM, шестерня, налаштування, табло цеху.

Логіку графіка стереже `test_power_schedule.py` на справжніх відповідях; тут —
що віджет СПРАВДІ доїжджає до розмітки (живий запит, а не самі дані), слухається
обох перемикачів і не зникає з DOM разом зі своїм поллом.

Знімок кладеться справжнім шляхом (`refresh` з відповіддю у форматі Yasno), а
день у ній — сьогоднішній: `live_view` бере справжній годинник.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.business_day import business_now
from app.services import power_schedule as ps
from app.services.widget_order import clean_hidden_widgets
from app.settings_store import get_setting
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура


@pytest.fixture(autouse=True)
def _clean_power():
    ps.reset()
    ps.configure("", False)
    yield
    ps.reset()
    ps.configure("", False)


def _yasno(slots, status="ScheduleApplies", group="4.1"):
    today = business_now().date()
    return {group: {
        "today": {"date": f"{today.isoformat()}T00:00:00+02:00", "status": status,
                  "slots": [{"start": a, "end": b, "type": "Definite"} for a, b in slots]},
        "tomorrow": {"date": f"{(today + timedelta(days=1)).isoformat()}T00:00:00+02:00",
                     "status": "WaitingForSchedule", "slots": []},
        "updatedOn": f"{today.isoformat()}T05:00:00+00:00",
    }}


def _install(slots, *, board=False, status="ScheduleApplies"):
    ps.configure("4.1", board)
    assert ps.refresh("4.1", lambda url: _yasno(slots, status)) is True


def _login(app, who=ADMIN):
    client = MiniClient(app)
    client.login(*who)
    return client


def _slot_tag(html: str) -> str:
    return html.split('id="power-widget"', 1)[1].split(">", 1)[0]


def _slot_hidden(html: str) -> bool:
    """Чи стоїть на обгортці атрибут `hidden`. Шукати слово «hidden» у тегу не
    можна: воно є в `hx-trigger` (`!document.hidden`), і перевірка «сховано»
    була б зеленою завжди."""
    return _slot_tag(html).rstrip().endswith("hidden")


# ── CRM ──────────────────────────────────────────────────────────────────────

def test_without_a_queue_number_the_slot_is_there_but_hidden(app_db):  # noqa: F811
    app, _ = app_db
    client = _login(app)
    for path in ("/?ready=all", "/mail", "/power/widget"):
        status, _, html = client.get(path)
        assert status == 200, path
        assert _slot_hidden(html) and 'hx-get="/power/widget"' in _slot_tag(html), path
        assert "data-power-toggle" not in html, path


def test_outage_reaches_queue_mail_and_the_poll_fragment(app_db):  # noqa: F811
    app, _ = app_db
    _install([(0, 1440)])
    client = _login(app, OPERATOR)
    for path in ("/?ready=all", "/mail", "/power/widget"):
        status, _, html = client.get(path)
        assert status == 200, path
        slot = html.split('id="power-widget"', 1)[1]
        assert not _slot_hidden(html), path
        for needle in ("data-power-toggle", "Світла немає", "до кінця доби", "черга 4.1",
                       "без світла 24 год", "ще не опубліковано", "оновлено сьогодні о 07:00"):
            assert needle in slot, (path, needle)
        panel = slot.split('id="power-panel"', 1)[1].split("pg-key", 1)[0]
        assert panel.count('class="is-off') == 24 and panel.count('class="is-wait') == 24, path
        assert panel.count("is-now") == 1, "рівно одна поточна година"


def test_day_without_outages_is_calm_and_lit(app_db):  # noqa: F811
    app, _ = app_db
    _install([], status="NoOutages")
    _, _, html = _login(app).get("/power/widget")
    assert "Світло є" in html and "сьогодні без відключень" in html
    assert 'class="pw-ico is-lit"' in html
    assert "is-soon" not in html and "is-alarm" not in html


def test_poll_fragment_needs_login(app_db):  # noqa: F811
    app, _ = app_db
    assert MiniClient(app).get("/power/widget")[0] == 401


def test_gear_hides_the_widget_for_this_account_only_and_keeps_the_poll(app_db):  # noqa: F811
    """Сховане в шестерні не повертається на першому ж поллі, але обгортка з
    поллом лишається — інакше увімкнути назад без перезавантаження не вийшло б."""
    app, _ = app_db
    _install([(0, 1440)])
    assert clean_hidden_widgets("power,чуже,sisma") == "sisma,power"
    admin, operator = _login(app, ADMIN), _login(app, OPERATOR)
    _, _, page = admin.get("/?ready=all")
    assert 'data-header-widget="power"' in page, "перемикач «Світло» є в шестерні черги"
    assert admin.post("/account/header-widgets", {"hidden": "power"})[0] == 204
    for path in ("/power/widget", "/?ready=all", "/mail"):
        _, _, html = admin.get(path)
        assert _slot_hidden(html) and 'hx-get="/power/widget"' in _slot_tag(html), path
    _, _, theirs = operator.get("/power/widget")
    assert not _slot_hidden(theirs), "це налаштування акаунта"
    assert admin.post("/account/header-widgets", {"hidden": ""})[0] == 204
    _, _, back = admin.get("/power/widget")
    assert not _slot_hidden(back)


# ── Налаштування ─────────────────────────────────────────────────────────────

def test_settings_save_queue_number_and_board_switch(app_db):  # noqa: F811
    app, factory = app_db
    client = _login(app)
    status, _, page = client.get("/settings/feedback")
    assert status == 200 and 'action="/settings/feedback/board/power"' in page

    client.post("/settings/feedback/board/power", {"group": " 4,1 ", "board": "1"})
    with factory() as db:
        assert get_setting(db, ps.GROUP_KEY) == "4.1" and get_setting(db, ps.BOARD_KEY) == "1"
    assert ps.configured_group() == "4.1" and ps.board_enabled() is True
    assert ps.wake.is_set(), "нова черга будить фоновий прохід, а не чекає 10 хв"
    _, _, page = client.get("/settings/feedback")
    assert 'value="4.1"' in page and 'name="board" value="1" checked' in page

    # Той самий «Зберегти» без прапорця — вимкнути смугу; черга лишається.
    client.post("/settings/feedback/board/power", {"group": "4.1"})
    with factory() as db:
        assert get_setting(db, ps.GROUP_KEY) == "4.1" and (get_setting(db, ps.BOARD_KEY) or "") == ""
    assert ps.board_enabled() is False

    # Не черга — нічого не зіпсовано.
    client.post("/settings/feedback/board/power", {"group": "Білицька", "board": "1"})
    with factory() as db:
        assert get_setting(db, ps.GROUP_KEY) == "4.1"
    assert ps.configured_group() == "4.1" and ps.board_enabled() is False

    client.post("/settings/feedback/board/power", {"group": ""})
    with factory() as db:
        assert (get_setting(db, ps.GROUP_KEY) or "") == ""
    assert ps.configured_group() == "" and ps.live_view() is None


def test_operator_cannot_change_the_queue_number(app_db):  # noqa: F811
    app, factory = app_db
    _login(app, OPERATOR).post("/settings/feedback/board/power", {"group": "4.1", "board": "1"})
    with factory() as db:
        assert (get_setting(db, ps.GROUP_KEY) or "") == ""
    assert ps.configured_group() == ""


def test_changing_the_queue_forgets_the_old_schedule():
    _install([(0, 1440)])
    assert ps.live_view().kind == "off"
    ps.configure("33.1", False)
    assert ps.current() == (None, None)
    assert ps.live_view().kind == "void", "графік попередньої черги не видається за новий"


def test_load_config_reads_both_settings(app_db):  # noqa: F811
    from app.settings_store import set_setting

    _, factory = app_db
    with factory() as db:
        set_setting(db, ps.GROUP_KEY, "33.1")
        set_setting(db, ps.BOARD_KEY, "1")
        db.commit()
        assert ps.load_config(db) == "33.1"
    assert ps.board_enabled() is True


# ── Табло цеху ───────────────────────────────────────────────────────────────

def _board(app_db, monkeypatch):
    from app.routers.furnace_board import create_board_app
    from app.services import furnace_board as fb
    from app.services import machines as machines_mod
    from tests.test_furnace_board import _enable

    _, factory = app_db
    token = _enable(factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(machines_mod, "snapshot", lambda db: [])
    monkeypatch.setattr(fb, "board_view", lambda db, now=None, cards=None: SimpleNamespace(
        cards=[], nearest_name="", nearest_at="", nearest_day="", nearest_iso=""))
    return MiniClient(create_board_app()), token


def test_board_strip_obeys_its_own_switch(app_db, monkeypatch):  # noqa: F811
    board, token = _board(app_db, monkeypatch)
    _install([(0, 1440)], board=False)
    for path in (f"/t/{token}/shop", f"/t/{token}/shop/cards"):
        assert 'class="pw-strip' not in board.get(path)[2], "у CRM увімкнено, на табло — ні"
    ps.configure("4.1", True)
    for path in (f"/t/{token}/shop", f"/t/{token}/shop/cards"):
        html = board.get(path)[2]
        strip = html.split('class="pw-strip', 1)[1].split('class="mgrid"', 1)[0]
        for needle in ("Світла немає", "до кінця доби", 'class="pw-off"', "pw-ticks"):
            assert needle in strip, (path, needle)
        assert "pw-alert" not in strip and "data-left-to" not in strip, "до кінця доби відліку немає"
    ps.configure("", True)
    assert 'class="pw-strip' not in board.get(f"/t/{token}/shop/cards")[2], "немає черги — немає смуги"


def test_board_warns_half_an_hour_before_the_cut(app_db, monkeypatch):  # noqa: F811
    now = business_now()
    minute = now.hour * 60 + now.minute
    if minute >= 1425:
        pytest.skip("за чверть години до півночі вимкнення вже не вміщається в сьогоднішню добу")
    board, token = _board(app_db, monkeypatch)
    cut = minute + 10
    _install([(cut, min(1440, cut + 120))], board=True)
    html = board.get(f"/t/{token}/shop/cards")[2]
    strip = html.split('class="pw-strip', 1)[1].split('class="mgrid"', 1)[0]
    assert "is-soon" in html.split('class="pw-strip', 1)[1].split('"', 1)[0]
    assert "Скоро вимкнуть" in strip and 'class="pw-alert"' in strip and "вимкнуть світло" in strip
    # `business_now()` — із поясом; відлік на табло йде без нього, як у печей.
    iso = (now.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=cut)).isoformat()
    assert strip.count(f'data-left-to="{iso}"') == 2, "відлік тікає і в смузі, і в попередженні"
    assert "до вимкнення" in strip
