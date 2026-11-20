"""Сторож матеріалу (`app/services/material_guard.py`, MATERIAL_GUARD_BRIEF.md).

Червона картка, коли диск програми не той, що замовлено. Випадки — зі звірки
цеху 18.11.26 (`kmill_disc_check days=7`): висота в межах похибки усадки
картки НЕ дає; збіг Sum3D ID з роботою кількох днів тому — теж ні;
Emotions ↔ Z-Nature і A4 ↔ A3.5 — дає; «Погодити весь диск» — для невірно
названого Monolith «A4» x05."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import ActionLog, MachineReading, MaterialGuardAck, Order, User
from app.services import disc_check
from app.services import material_guard as mg
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

NOW = datetime(2026, 11, 18, 4, 0)
TAB = "17.11.26"


@pytest.fixture(autouse=True)
def _fresh_cache():
    mg.invalidate()
    yield
    mg.invalidate()


@pytest.fixture
def tracks(tmp_path, monkeypatch):
    """Кеш слідів програм — як його пише `program_tracks` (без тек верстатів)."""
    folder = tmp_path / "program_tracks"
    folder.mkdir()
    from app.services import program_tracks

    monkeypatch.setattr(program_tracks, "cache_dir", lambda: folder)
    monkeypatch.setattr(program_tracks, "known_folders", lambda db: [])
    return folder


def _track(folder, name: str, source: str = r"\\250i-sec\programs") -> None:
    (folder / f"{name.lower()}.json").write_text(json.dumps({"name": name, "source": source}), encoding="utf-8")


def _order(db: Session, material: str, sum3d: str, *, tab: str = TAB, client="Маріанна Голій", qty="4") -> Order:
    order = Order(source="sheet_client", client_name=client, material_color=material, quantity=qty,
                  sum3d_id=sum3d, sheet_tab=tab, created_at=NOW - timedelta(hours=3))
    db.add(order)
    db.commit()
    return order


def _disc(name: str) -> disc_check.ProgramDisc:
    disc = disc_check.program_disc(name)
    assert disc is not None
    return disc


# ── Що вважаємо помилкою ────────────────────────────────────────────────────


def test_emotions_vs_z_nature_is_a_line_mismatch(db_session):
    order = _order(db_session, "emo a3", "02-24-01")
    reasons = mg.disc_reasons(_disc("2_16-Z-Nature-A3-x12_2026-11-17_02-24-01.iso"), order)
    assert reasons and reasons[0].startswith("лінія: замовлено Emotions, диск Z-Nature")


def test_wrong_shade_is_caught(db_session):
    order = _order(db_session, "mono a3.5", "16-07-15")
    reasons = mg.disc_reasons(_disc("2_16-Monolith-A4-x05_2026-11-17_16-07-15.iso"), order)
    assert any(r.startswith("колір") for r in reasons)


def test_height_never_raises_a_card(db_session):
    """9 із 20 розбіжностей звірки — висота в межах похибки усадки."""
    order = _order(db_session, "emo a3", "21-59-27")
    order.stl_height_mm = 11.2  # ≈14.0 мм після усадки на диску 14 мм
    db_session.commit()
    assert mg.disc_reasons(_disc("9_14-Emotions-A3-x861PRO_2026-11-17_21-59-27.iso"), order) == []


def test_matching_disc_is_silent(db_session):
    order = _order(db_session, "mono a2", "01-20-28")
    assert mg.disc_reasons(_disc("1_16-Monolith-A2-x26_2026-11-17_01-20-28.iso"), order) == []


def test_old_work_with_the_same_sum3d_id_is_not_matched(db_session):
    """8241 (18.11.26): ПММА 12.11 і цирконієва програма 17.11 з тим самим часом."""
    _order(db_session, "pmma a2", "20-56-55", tab="12.11.26")
    assert mg.orders_on_day(db_session, _disc("2_12-Monolith-A3-5-x14_2026-11-17_20-56-55.iso")) == []


# ── Звідки програми ─────────────────────────────────────────────────────────


def test_program_in_folder_raises_a_card_before_the_machine(db_session, tracks):
    _order(db_session, "emo a3", "02-24-01")
    _track(tracks, "2_16-Z-Nature-A3-x12_2026-11-17_02-24-01.iso")
    alerts = mg.all_alerts(db_session, NOW, use_cache=False)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.layer == mg.LAYER_DISC and alert.ordered_label == "Emotions A3"
    assert alert.actual_label == "Z-Nature A3" and alert.where == "тека програм"


def test_program_folder_is_named_after_its_machine(db_session, tracks, monkeypatch):
    """Тека програм відома (Налаштування → Верстати) — картка називає верстат,
    а не «тека програм». (Перша версія брала неіснуюче поле — mypy спіймав.)"""
    from app.services import program_tracks

    monkeypatch.setattr(program_tracks, "known_folders",
                        lambda db: [program_tracks.MachineFolder("250i-Sec", r"\\250i-sec\programs", ".iso")])
    _order(db_session, "emo a3", "02-24-01")
    _track(tracks, "2_16-Z-Nature-A3-x12_2026-11-17_02-24-01.iso", source="\\\\250I-SEC\\Programs\\")
    assert [a.where for a in mg.all_alerts(db_session, NOW, use_cache=False)] == ["250i-Sec"]


def test_machine_reading_is_the_fallback_source(db_session, tracks):
    from app.models import Machine

    db_session.add(Machine(name="250i-Sec", host="192.168.1.113", port=8765, created_at=NOW))
    db_session.add(MachineReading(host="192.168.1.113-8765", captured_at=NOW - timedelta(hours=1),
                                  iso_name="2_16-Monolith-A4-x05_2026-11-17_16-07-15.iso"))
    db_session.commit()
    _order(db_session, "mono a3.5", "16-07-15")
    alerts = mg.all_alerts(db_session, NOW, use_cache=False)
    assert [a.where for a in alerts] == ["250i-Sec"]


def test_old_programs_are_outside_the_window(db_session, tracks):
    import os

    _order(db_session, "emo a3", "02-24-01")
    _track(tracks, "2_16-Z-Nature-A3-x12_2026-11-17_02-24-01.iso")
    old = (NOW - timedelta(hours=mg.RECENT_HOURS + 2)).timestamp()
    for path in tracks.iterdir():
        os.utime(path, (old, old))
    assert mg.all_alerts(db_session, NOW, use_cache=False) == []


# ── Погоджено ───────────────────────────────────────────────────────────────


def test_agree_one_work_hides_only_it_and_leaves_a_trace(db_session, tracks):
    user = User(username="op", password_hash="x")
    db_session.add(user)
    first = _order(db_session, "mono a3.5", "16-07-15")
    second = _order(db_session, "mono a3.5", "16-07-15", client="Неда")
    _track(tracks, "2_16-Monolith-A4-x05_2026-11-17_16-07-15.iso")
    assert len(mg.all_alerts(db_session, NOW, use_cache=False)) == 2
    mg.acknowledge(db_session, user, layer=mg.LAYER_DISC, order_id=first.id,
                   disc_key="16-monolith-a4-x05", whole_disc=False)
    left = mg.all_alerts(db_session, NOW, use_cache=False)
    assert [a.order_id for a in left] == [second.id]
    log = db_session.query(ActionLog).filter_by(order_id=first.id, action_type="material_guard_ack").one()
    assert log.operator_id == user.id and "погоджено цю роботу" in log.note


def test_agree_whole_disc_for_a_misnamed_disc(db_session, tracks):
    """Власник 18.11.26: Monolith «A4» x05 — невірно названий диск (A3.5)."""
    user = User(username="op", password_hash="x")
    db_session.add(user)
    first = _order(db_session, "mono a3.5", "16-07-15")
    _order(db_session, "mono a3.5", "16-07-15", client="Неда")
    _track(tracks, "2_16-Monolith-A4-x05_2026-11-17_16-07-15.iso")
    mg.acknowledge(db_session, user, layer=mg.LAYER_DISC, order_id=first.id,
                   disc_key="16-monolith-a4-x05", whole_disc=True)
    assert mg.all_alerts(db_session, NOW, use_cache=False) == []
    ack = db_session.query(MaterialGuardAck).one()
    assert ack.order_id is None and ack.disc_key == "16-monolith-a4-x05"
    # Нова робота на тому самому диску пізніше — теж без картки.
    _order(db_session, "mono a3.5", "18-00-00", client="R")
    _track(tracks, "3_16-Monolith-A4-x05_2026-11-17_18-00-00.iso")
    assert mg.all_alerts(db_session, NOW, use_cache=False) == []


def test_whole_disc_without_a_disc_is_refused(db_session):
    with pytest.raises(ValueError):
        mg.acknowledge(db_session, None, layer=mg.LAYER_DISC, order_id=1, disc_key="", whole_disc=True)


# ── Перемикачі на оператора ─────────────────────────────────────────────────


def test_operator_sees_only_the_layers_switched_on(db_session, tracks, monkeypatch):
    disc = mg.Alert(layer=mg.LAYER_DISC, order_id=1, client="", quantity="", ordered="", ordered_label="",
                    actual="", actual_label="")
    letter = mg.Alert(layer=mg.LAYER_LETTER, order_id=2, client="", quantity="", ordered="", ordered_label="",
                      actual="", actual_label="")
    monkeypatch.setattr(mg, "all_alerts", lambda db: [disc, letter])
    user = User(username="op", password_hash="x")
    assert mg.alerts_for(db_session, user) == []
    user.material_guard = True
    assert mg.alerts_for(db_session, user) == [disc]
    user.material_guard_letter = True
    assert mg.alerts_for(db_session, user) == [disc, letter]
    user.material_guard = False
    assert mg.alerts_for(db_session, user) == [letter]


# ── Екран ───────────────────────────────────────────────────────────────────


def _alert(**kw) -> mg.Alert:
    base = dict(layer=mg.LAYER_DISC, order_id=7, client="Маріанна Голій", quantity="4", ordered="emo a3",
                ordered_label="Emotions A3", actual="2_16-Z-Nature-A3-x12", actual_label="Z-Nature A3",
                reasons=["лінія: замовлено Emotions, диск Z-Nature"], disc_key="16-z-nature-a3-x12",
                program="2_16-Z-Nature-A3-x12_2026-11-17_02-24-01.iso", where="250i-Sec", seen_at=NOW)
    base.update(kw)
    return mg.Alert(**base)


@pytest.mark.parametrize("view, marker", [("banner", "mguard-banner"), ("card", "mguard-card")])
def test_both_views_render_with_both_buttons(view, marker):
    from app.routers.deps import templates

    html = templates.get_template("_material_guard.html").render(mg_alerts=[_alert()], mg_view=view)
    assert marker in html and 'data-orders="7"' in html
    assert "Відкрити роботу" in html and "Погоджено" in html and "Погодити весь диск" in html
    assert "Z-Nature A3" in html and "Emotions A3" in html


def test_empty_slot_still_tells_queue_js_to_clear_rows():
    from app.routers.deps import templates

    html = templates.get_template("_material_guard.html").render(mg_alerts=[], mg_view="banner")
    assert 'data-orders=""' in html and "mguard-banner" not in html


def test_account_switches_save_only_their_own_field(app_db):  # noqa: F811
    app, factory = app_db
    client = MiniClient(app)
    client.login(*OPERATOR)
    assert client.post("/account/material-guard", {"guard": "on"})[0] == 204
    assert client.post("/account/material-guard", {"view": "card"})[0] == 204
    assert client.post("/account/material-guard", {"letter": "maybe"})[0] == 422
    with factory() as db:
        user = db.query(User).filter_by(username=OPERATOR[0]).one()
        assert user.material_guard is True and user.material_guard_letter is False
        assert user.material_guard_view == "card"
    assert client.post("/account/material-guard", {"guard": "off"})[0] == 204
    with factory() as db:
        user = db.query(User).filter_by(username=OPERATOR[0]).one()
        assert user.material_guard is False and user.material_guard_view == "card"


# ── Доступ від адміна (власник 20.11.26) ────────────────────────────────────


def test_admin_access_overrides_operator_switches(db_session, monkeypatch):
    disc = mg.Alert(layer=mg.LAYER_DISC, order_id=1, client="", quantity="", ordered="", ordered_label="",
                    actual="", actual_label="")
    letter = mg.Alert(layer=mg.LAYER_LETTER, order_id=2, client="", quantity="", ordered="", ordered_label="",
                      actual="", actual_label="")
    monkeypatch.setattr(mg, "all_alerts", lambda db: [disc, letter])
    user = User(username="op", password_hash="x", material_guard=True, material_guard_letter=True)
    # Порожньо (стара база, ніхто не чіпав) = на вибір оператора, як було.
    assert mg.alerts_for(db_session, user) == [disc, letter]
    user.material_guard_access = "deny"
    assert mg.alerts_for(db_session, user) == []
    user.material_guard_access = "force"
    user.material_guard = False
    user.material_guard_letter = False
    # «Завжди» вмикає основну перевірку, але не «лист ↔ вписане».
    assert mg.alerts_for(db_session, user) == [disc]
    user.material_guard_access = "щось"
    assert mg.access_of(user) == mg.ACCESS_CHOICE and mg.alerts_for(db_session, user) == []


def _access(factory, username: str) -> str:
    with factory() as db:
        return db.query(User).filter_by(username=username).one().material_guard_access


def test_admin_sets_access_and_operator_switches_obey(app_db):  # noqa: F811
    app, factory = app_db
    with factory() as db:
        operator_id = db.query(User).filter_by(username=OPERATOR[0]).one().id
    admin = MiniClient(app)
    admin.login(*ADMIN)
    url = f"/settings/users/{operator_id}/feature-access"

    status, _, page = admin.get("/settings")
    assert status == 200 and f'action="{url}"' in page and '<option value="force"' in page
    assert 'name="feature" value="material_guard"' in page

    def set_access(word: str) -> int:
        return admin.post(url, {"feature": "material_guard", "access": word})[0]

    assert set_access("deny") == 303
    assert _access(factory, OPERATOR[0]) == "deny"
    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert operator.post("/account/material-guard", {"guard": "on"})[0] == 403
    assert operator.post("/account/material-guard", {"letter": "on"})[0] == 403
    assert operator.post("/account/material-guard", {"view": "card"})[0] == 204
    assert operator.post("/material-guard/ack", {"layer": "disc", "order_id": "1"})[0] == 403
    _, _, account = operator.get("/account")
    assert "Адміністратор вимкнув для вас сторож матеріалу" in account
    assert 'data-mguard="guard"' not in account

    assert set_access("force") == 303
    assert operator.post("/account/material-guard", {"guard": "off"})[0] == 403
    assert operator.post("/account/material-guard", {"letter": "on"})[0] == 204
    _, _, account = operator.get("/account")
    assert "Увімкнено адміністратором" in account

    assert set_access("choice") == 303
    assert _access(factory, OPERATOR[0]) == ""
    assert operator.post("/account/material-guard", {"guard": "off"})[0] == 204
    # Невідоме слово, порожнє поле й невідома функція нічого не змінюють.
    assert set_access("") == 303
    assert set_access("maybe") == 303
    assert admin.post(url, {"feature": "nope", "access": "force"})[0] == 303
    assert _access(factory, OPERATOR[0]) == ""
    # Оператор сам собі доступу не дасть.
    assert operator.post(url, {"feature": "material_guard", "access": "force"})[0] == 403
    assert _access(factory, OPERATOR[0]) == ""


def test_agree_button_request_records_and_answers_with_the_slot(app_db):  # noqa: F811
    """Те, що шле кнопка (`hx-vals` форма): запис «Погоджено» і новий слот."""
    app, factory = app_db
    with factory() as db:
        order = Order(source="sheet_client", client_name="Неда", material_color="mono a3.5",
                      quantity="1", sum3d_id="16-07-15", sheet_tab=TAB)
        db.add(order)
        db.commit()
        order_id = order.id
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.post("/material-guard/ack", {"layer": "disc", "order_id": str(order_id),
                                                          "disc_key": "16-monolith-a4-x05", "scope": "disc"})
    assert status == 200 and 'class="mguard"' in html
    assert client.post("/material-guard/ack", {"layer": "disc", "order_id": str(order_id),
                                               "disc_key": "x", "scope": "everything"})[0] == 422
    with factory() as db:
        ack = db.query(MaterialGuardAck).one()
        assert ack.disc_key == "16-monolith-a4-x05" and ack.order_id is None and ack.operator_id is not None


def test_slot_is_empty_for_an_operator_with_the_guard_off(app_db):  # noqa: F811
    app, _ = app_db
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.get("/material-guard/slot")
    assert status == 200 and 'data-orders=""' in html


def test_account_page_shows_the_switches(app_db):  # noqa: F811
    app, _ = app_db
    client = MiniClient(app)
    client.login(*OPERATOR)
    status, _, html = client.get("/account")
    assert status == 200 and "Сторож матеріалу" in html and 'data-mguard="letter"' in html
