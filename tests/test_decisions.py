"""Журнал рішень (власник 24.11.26): що система запропонувала, що зробив оператор,
що сталося далі, і чого з цього можна навчитись."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app import decisions_store as store
from app.models import ActionLog, EmailMessage, Order, ReworkRecord, User
from app.services import decision_learn as learn
from app.services import decision_observer as observer
from app.services import decision_rules as rules
from app.services import decision_story, decisions
from tests.conftest import make_memory_engine
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура


@pytest.fixture
def journal(tmp_path):
    decisions.reset_for_tests()
    path = tmp_path / "decisions.db"
    store.configure(path)
    yield path
    store.shutdown()
    decisions.reset_for_tests()


@pytest.fixture
def db():
    with Session(make_memory_engine(), expire_on_commit=False) as session:
        yield session


def _events(step: str | None = None) -> list[dict]:
    store.flush()
    rows = store.query("SELECT * FROM events ORDER BY id")
    return [r for r in rows if step is None or r["step"] == step]


def _letter(db, **fields) -> EmailMessage:
    email = EmailMessage(uid=fields.pop("uid", "u1"), attachments_status="ready", **fields)
    db.add(email)
    db.commit()
    return email


def _order(db, **fields) -> Order:
    fields.setdefault("source", "email")
    fields.setdefault("sheet_tab", "24.11.26")
    order = Order(**fields)
    db.add(order)
    db.commit()
    return order


# ── Сховище ────────────────────────────────────────────────────────────────


def test_without_configure_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "default_path", lambda: tmp_path / "none.db")
    store.put("крок", "система", email_id=1)
    assert not (tmp_path / "none.db").exists()
    assert store.query("SELECT * FROM events") == []


def test_broken_file_never_reaches_the_caller(tmp_path):
    path = tmp_path / "decisions.db"
    path.write_bytes(b"this is not a database" * 100)
    store.configure(path)
    try:
        store.put("крок", "система", email_id=1)
        store.flush()
    finally:
        store.shutdown()


def test_derived_event_is_written_once(journal):
    for _ in range(2):
        store.put("переробка", "таблиця", order_id=1, src="rework:1")
    assert len(_events()) == 1


def test_snapshot_is_a_valid_copy_and_once_a_day(journal, tmp_path):
    store.put("крок", "система")
    store.flush()
    copy = store.snapshot(tmp_path / "copies")
    assert copy is not None and store.snapshot(tmp_path / "copies") is None
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


# ── Запис ──────────────────────────────────────────────────────────────────


def _card(**extra):
    context = {
        "client_name": "Люмі-Дент", "material_color": "mono a3", "quantity": "4",
        "qty_units": SimpleNamespace(units=4), "qty_auto": True, "qty_letter": None,
        "unclaimed_attachments": [
            SimpleNamespace(filename="Іваненко Петро-11-12-crown_cad.stl"),
            SimpleNamespace(filename="2026-11-24_00001-001-11-Petrenko_cad.stl"),
        ],
    }
    context.update(extra)
    return context


def test_card_is_recorded_as_shown_without_names(journal):
    email = SimpleNamespace(id=7, material_color_guess="ПММА для Іваненка")
    decisions.note_card(email, SimpleNamespace(id=2), _card(), sure_material="mono a3")
    decisions.note_card(email, SimpleNamespace(id=2), _card(), sure_material="mono a3")
    (row,) = _events(decisions.STEP_CARD)
    assert row["proposed"]["матеріал"] == "mono a3" and row["proposed"]["кількість"] == "4"
    assert row["data"]["впевнений_матеріал"] == "mono a3"
    types = [m["тип"] for m in row["data"]["файли"]["моделі"]]
    assert types == ["crown", "інше"]
    raw = journal.read_bytes() + json.dumps(row, ensure_ascii=False, default=str).encode()
    for name in ("Іванен", "Petrenko", "Петро"):
        assert name.encode() not in raw


def test_card_with_operator_values_changed_is_a_new_record(journal):
    email = SimpleNamespace(id=7, material_color_guess=None)
    decisions.note_card(email, None, _card())
    decisions.note_card(email, None, _card(quantity="5"))
    assert [r["proposed"]["кількість"] for r in _events(decisions.STEP_CARD)] == ["4", "5"]


def test_accept_route_service_records_what_operator_left(journal, tmp_path, monkeypatch):
    from app.services import mail_accept as svc
    from tests.test_mail_transaction_safety import _database, _letter as letter, _user, _wire

    engine = _database()
    _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as session:
        email, _ = letter(session, tmp_path / "spool" / "u1")
        result = svc.accept_letter(session, _user(session), email, client_name="Люмі-Дент",
                                   material_color="mono a3.5", quantity="5", opak="2")
        assert result.ok, result.error
        (row,) = _events(decisions.STEP_ACCEPT)
        assert row["email_id"] == email.id and row["order_id"] == result.order.id
    assert row["chosen"] == {"клієнт": "Люмі-Дент", "матеріал": "mono a3.5", "кількість": "5", "опак": "2", "sum3d": ""}


def test_recording_failure_never_breaks_the_accept(journal, tmp_path, monkeypatch):
    from app.services import mail_accept as svc
    from tests.test_mail_transaction_safety import _database, _letter as letter, _user, _wire

    def boom(*_a, **_k):
        raise RuntimeError("журнал зламався")

    monkeypatch.setattr(store, "put", boom)
    engine = _database()
    _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as session:
        email, _ = letter(session, tmp_path / "spool" / "u1")
        result = svc.accept_letter(session, _user(session), email, client_name="Х", material_color="mono a3")
        assert result.ok, result.error


# ── Спостерігач ────────────────────────────────────────────────────────────


def test_first_pass_only_remembers(journal, db):
    _order(db, quantity="4")
    _letter(db, status="нове")
    assert observer.observe(db) == 0
    assert _events() == []
    assert observer.observe(db) == 0


def test_sheet_operator_and_unknown_changes_are_told_apart(journal, db):
    by_operator = _order(db, quantity="4")
    by_sheet = _order(db, quantity="4")
    unknown = _order(db, quantity="4")
    user = User(username="op", password_hash="x")
    db.add(user)
    db.commit()
    observer.observe(db)

    for order in (by_operator, by_sheet, unknown):
        order.quantity = "5"
    by_sheet.sheet_changed_at = datetime.now()
    db.add(ActionLog(order_id=by_operator.id, operator_id=user.id, action_type="quantity", field="quantity"))
    db.commit()
    observer.observe(db)

    rows = {r["order_id"]: r for r in _events(decisions.STEP_FIELD)}
    assert rows[by_operator.id]["actor"] == "оператор" and rows[by_operator.id]["user_id"] == user.id
    assert rows[by_sheet.id]["actor"] == "таблиця"
    assert rows[unknown.id]["actor"] == "невідомо"
    assert {(r["rule"], r["proposed"], r["chosen"]) for r in rows.values()} == {("кількість", "4", "5")}
    observer.observe(db)
    assert len(_events(decisions.STEP_FIELD)) == 3


def test_change_made_while_the_program_was_off_is_not_lost(journal, db):
    order = _order(db, quantity="4")
    observer.observe(db)
    store.shutdown()
    order.quantity = "6"
    db.commit()
    store.configure(journal)
    observer.observe(db)
    (row,) = _events(decisions.STEP_FIELD)
    assert (row["proposed"], row["chosen"]) == ("4", "6")


def test_letter_fate_names_the_person_only_when_the_route_did(journal, db):
    rejected = _letter(db, uid="a", status="нове")
    gone = _letter(db, uid="b", status="нове")
    observer.observe(db)
    decisions.note_actor("letter", rejected.id, 3)
    rejected.status = "відхилено"
    gone.inbox_gone_at = datetime.now()
    db.commit()
    observer.observe(db)
    rows = {r["email_id"]: r for r in _events(decisions.STEP_FATE)}
    assert (rows[rejected.id]["chosen"], rows[rejected.id]["user_id"]) == ("відхилено", 3)
    assert (rows[gone.id]["chosen"], rows[gone.id]["actor"]) == ("покинув Вхідні", "невідомо")


def test_hold_reason_is_kept_but_free_text_is_not(journal, db):
    email = _letter(db, status="нове")
    observer.observe(db)
    email.hold_at, email.hold_reason, email.hold_note = datetime.now(), "no_files", "Петренко Іван"
    db.commit()
    observer.observe(db)
    assert _events(decisions.STEP_FATE)[0]["chosen"] == "на уточненні: no_files"
    store.flush()
    assert "Петренко".encode() not in journal.read_bytes()


def test_rework_is_recorded_once_with_dictionary_blame(journal, db):
    order = _order(db)
    observer.observe(db)
    db.add(ReworkRecord(order_id=order.id, occurrence=2, blame="технік Петренко"))
    db.commit()
    observer.observe(db)
    observer.observe(db)
    (row,) = _events(decisions.STEP_REWORK)
    assert row["data"] == {"який": 2, "винен": "технік"}


# ── Навчені правила ────────────────────────────────────────────────────────

TWICE = ["a-47-crown_cad.stl", "b-47-crown_cad.stl"]
WAXUP = ["a-47-crown_cad.stl", "b-47-waxup_cad.stl"]


def test_repeat_rule_counts_only_its_own_pair_of_types():
    assert rules.repeat_types(TWICE) == {47: "crown+crown"}
    assert rules.repeat_extra(TWICE, {"crown+crown"}) == 1
    assert rules.repeat_extra(WAXUP, {"crown+crown"}) == 0
    assert rules.repeat_extra(TWICE, set()) == 0


def _rule(kind, params, key="k1"):
    return {"key": key, "kind": kind, "title": "правило", "params": params}


def test_without_decisions_rules_change_nothing(db):
    applied = rules.apply(db, client_name="Х", system_material="mono a3", filenames=TWICE)
    assert applied == rules.Applied()


@pytest.mark.parametrize("status, extra", [("on", 1), ("shadow", 0), ("off", 0), ("paused", 0)])
def test_only_enabled_rule_touches_the_quantity(db, status, extra):
    rules.save_status(db, _rule("repeat", {"types": "crown+crown"}), status, user_id=1)
    applied = rules.apply(db, client_name="Х", system_material=None, filenames=TWICE)
    assert applied.extra_units == extra
    assert bool(applied.shadow) == (status == "shadow")


def test_swap_rule_fires_only_for_its_client_and_its_material(db):
    from app.material_classifier import match_key

    params = {"client": "люмі-дент", "from": match_key("mono a3"), "to": "mono a3.5"}
    rules.save_status(db, _rule("swap", params), "on", user_id=1)

    def got(client, system):
        return rules.apply(db, client_name=client, system_material=system, filenames=[]).material

    assert got("Люмі-Дент", "mono a3") == "mono a3.5"
    assert got("Люмі-Дент", "mono a2") is None
    assert got("Люмі-Дент", None) is None
    assert got("Інший", "mono a3") is None


def test_broken_setting_means_no_rules(db):
    from app.settings_store import set_setting

    set_setting(db, rules.SETTING, "{зламано")
    assert rules.apply(db, client_name="Х", system_material=None, filenames=TWICE) == rules.Applied()


def _row(day, order, *, client="Клієнт", system="mono a3", chosen="mono a3", quantity="1", files=()):
    return {"at": datetime(2026, 11, 1) + timedelta(days=day), "email": order, "order": order, "client": client,
            "system": system, "chosen": chosen, "quantity": quantity, "files": list(files)}


def test_mining_finds_the_repeat_and_keeps_the_other_pair_out(db):
    rows = [_row(i, i, quantity="2", files=TWICE) for i in range(10)]
    rows += [_row(i, 100 + i, quantity="1", files=WAXUP) for i in range(10)]
    rows.sort(key=lambda r: r["at"])
    found = learn.mine(db, rows=rows)["candidates"]
    assert [c["params"] for c in found] == [{"types": "crown+crown"}]
    assert found[0]["train"]["проти"] == 0 and found[0]["test"]["за"] > 0


def test_candidate_that_fails_the_exam_is_not_offered(db):
    rows = [_row(i, i, chosen="mono a3.5") for i in range(7)]
    rows += [_row(7 + i, 7 + i, chosen="mono a3") for i in range(3)]
    assert learn.mine(db, rows=rows)["candidates"] == []


def test_client_habit_is_found_only_with_enough_proof(db):
    many = [_row(i, i, chosen="mono a3.5") for i in range(10)]
    (found,) = learn.mine(db, rows=many)["candidates"]
    assert found["kind"] == "swap" and found["params"]["to"] == "mono a3.5"
    assert learn.mine(db, rows=many[:2])["candidates"] == []


def test_empty_card_candidate(db):
    rows = [_row(i, i, system="", chosen="титан") for i in range(10)]
    (found,) = learn.mine(db, rows=rows)["candidates"]
    assert found["kind"] == "empty" and found["params"]["to"] == "титан"


def test_mcp_candidates_carry_no_client_name(db):
    rows = [_row(i, i, chosen="mono a3.5", client="Таємний Клієнт") for i in range(10)]
    public = learn.public_found(learn.mine(db, rows=rows))
    assert "аємний" not in json.dumps(public, ensure_ascii=False).casefold()


def _use(email_id, quantity_shown, quantity_taken, *, rule=None, shadow=None):
    email = SimpleNamespace(id=email_id, material_color_guess=None)
    decisions.note_card(email, None, _card(quantity=quantity_shown), rules=rule, shadow=shadow)
    decisions.note_accept(None, email, None, mode="card", client_name="Люмі-Дент", material_color="mono a3",
                          quantity=quantity_taken, opak="", sum3d_id="", picked_files=0, partial=False)


def test_usage_counts_kept_fixed_and_shadow(journal):
    _use(1, "5", "5", rule={"кількість": "k1"})
    _use(2, "5", "4", rule={"кількість": "k1"})
    _use(3, "4", "5", shadow={"кількість": {"правило": "k2", "додало_б": "1"}})
    _events()
    assert learn.usage() == {"k1": {"залишили": 1, "виправили": 1}, "k2": {"залишили": 1, "виправили": 0}}


def test_rule_that_operators_keep_fixing_turns_itself_off(journal, db):
    rules.save_status(db, _rule("repeat", {"types": "crown+crown"}), "on", user_id=1,
                      now=datetime.now() - timedelta(days=1))
    for i in range(6):
        _use(i + 1, "5", "4", rule={"кількість": "k1"})
    _events()
    assert learn.pause_bad(db) == ["k1"]
    assert rules.load(db)["k1"]["status"] == "paused"
    # Адмін увімкнув знову — стара лічба не вимикає правило за хвилину.
    rules.save_status(db, _rule("repeat", {"types": "crown+crown"}), "on", user_id=1,
                      now=datetime.now() + timedelta(seconds=5))
    assert learn.pause_bad(db) == []


# ── «Чому так» і екрани ────────────────────────────────────────────────────


def test_story_tells_what_the_operator_changed(journal, db):
    email = _letter(db, status="нове")
    order = _order(db, source_email_id=email.id, quantity="5")
    decisions.note_card(email, None, _card(), sure_material="mono a3")
    decisions.note_accept(None, email, order, mode="card", client_name="Люмі-Дент", material_color="mono a3.5",
                          quantity="5", opak="", sum3d_id="", picked_files=0, partial=False)
    _events()
    lines = decision_story.story(db, order)
    assert [line.title for line in lines] == ["Картка запропонувала", "Прийнято"]
    assert lines[1].changed == ["матеріал: mono a3 → mono a3.5", "кількість: 4 → 5"]


def test_story_of_one_color_part_does_not_show_the_other(journal, db):
    email = _letter(db, status="нове")
    first, second = _order(db, source_email_id=email.id), _order(db, source_email_id=email.id)
    for order in (first, second):
        decisions.note_accept(None, email, order, mode="card", client_name="Х", material_color=f"mono {order.id}",
                              quantity="1", opak="", sum3d_id="", picked_files=0, partial=True)
    _events()
    assert [line.detail for line in decision_story.story(db, first)] == [f"mono {first.id} · 1 од."]


def test_old_order_without_events_has_an_empty_story(journal, db):
    assert decision_story.story(db, _order(db)) == []


@pytest.fixture
def site(journal, app_db):  # noqa: F811
    # `app_db` підміняє й базу гейту ліцензії: без цього тест жив dev-базою й падав на CI.
    app, factory = app_db
    with factory() as session:
        session.add(Order(source="lab", sheet_tab="24.11.26", work_order_no="1"))
        session.commit()
    return lambda who: _login(MiniClient(app), ADMIN if who == "boss" else OPERATOR)


class _Site:
    """MiniClient повертає (код, заголовки, текст) — іменуємо для читабельності."""

    def __init__(self, client):
        self._client = client

    def get(self, path):
        return _named(self._client.get(path))

    def post(self, path, data=None):
        return _named(self._client.post(path, data))


def _named(answer):
    return SimpleNamespace(status_code=answer[0], text=answer[-1])


def _login(client, who):
    client.login(*who)
    return _Site(client)


CANDIDATE = {
    "key": "repeat:1", "kind": "repeat", "title": "Той самий зуб у файлах crown + crown — рахувати кожен файл",
    "params": {"types": "crown+crown"}, "train": {"за": 6, "проти": 0}, "test": {"за": 3, "проти": 0}, "examples": [1],
}


def test_learning_screen_turns_a_candidate_into_a_rule_and_back(site):
    learn.save_found({"at": "2026-11-24T04:00:00", "days": 90, "letters": 12, "exam_from": None,
                      "candidates": [CANDIDATE]})
    boss = site("boss")
    page = boss.get("/learning")
    assert page.status_code == 200 and "crown + crown" in page.text and "Увімкнути" in page.text
    assert "навчання <b>6</b>" in page.text

    body = boss.post("/learning/rule", {"key": "repeat:1", "status": "on"})
    assert body.status_code == 200 and "увімкнено" in body.text and "Нових закономірностей" in body.text
    body = boss.post("/learning/rule", {"key": "repeat:1", "status": "off"})
    assert "відхилено" in body.text
    body = boss.post("/learning/rule", {"key": "repeat:1", "status": "forget"})
    assert "Твої рішення" not in body.text and "Пробувати мовчки" in body.text
    assert boss.post("/learning/rule", {"key": "немає", "status": "on"}).status_code == 404
    assert boss.post("/learning/rule", {"key": "repeat:1", "status": "дурня"}).status_code == 400


def test_operator_cannot_open_or_switch_rules(site):
    op = site("op")
    assert op.get("/learning").status_code == 403
    assert op.post("/learning/rule", {"key": "repeat:1", "status": "on"}).status_code == 403
    assert op.post("/learning/recount").status_code == 403


def test_why_fragment_for_any_signed_in_user(site):
    page = site("op").get("/orders/1/why")
    assert page.status_code == 200 and "журнал почався пізніше" in page.text
    assert "/orders/1/why" in site("op").get("/orders/1").text


# ── Справжня картка листа ──────────────────────────────────────────────────


@pytest.fixture
def card(tmp_path, monkeypatch, journal):
    from app.routers import mail as mail_router
    from tests.test_mail_letter_signals import _email
    from tests.test_mail_transaction_safety import _database, _user, _wire

    engine = _database()
    _, mail_root = _wire(monkeypatch, tmp_path)
    (mail_root / "u1").mkdir(parents=True, exist_ok=True)
    with Session(engine, expire_on_commit=False) as session:
        email = _email(session, mail_root / "u1", "робота", "", TWICE)
        # Однаковий вміст = дублікат файлу: його картка й так не рахує.
        for attachment in email.attachments:
            (mail_root / "u1" / attachment.filename).write_bytes(attachment.filename.encode())
        user = _user(session)
        yield session, lambda **values: mail_router._mail_panel_context(session, email, user, **values)


def test_real_card_is_the_same_until_a_rule_is_enabled(card):
    session, render = card
    assert render()["quantity"] == "1"
    rules.save_status(session, _rule("repeat", {"types": "crown+crown"}), "shadow", user_id=1)
    assert render()["quantity"] == "1"
    rules.save_status(session, _rule("repeat", {"types": "crown+crown"}), "on", user_id=1)
    context = render()
    assert context["quantity"] == "2" and context["qty_auto"] and "+1" in context["qty_units"].summary
    assert set(context["att_types"].values()) == {"crown"}
    first, second, third = _events(decisions.STEP_CARD)
    assert first["rule"] is None and second["data"]["тінь"]["кількість"]["додало_б"] == "1"
    assert json.loads(third["rule"]) == {"кількість": "k1"}


def test_operators_own_numbers_are_never_touched_nor_recorded_as_shown(card):
    session, render = card
    rules.save_status(session, _rule("repeat", {"types": "crown+crown"}), "on", user_id=1)
    context = render(client_name="Х", material_color="mono a2", quantity="7", error="помилка")
    assert context["quantity"] == "7" and context["material_color"] == "mono a2"
    assert _events(decisions.STEP_CARD) == []


@pytest.mark.parametrize("target, shades, fits", [
    ("mono a2", [], True), ("mono a2", ["a2"], True), ("mono a2", ["a3"], False),
    ("mono a2", ["a2", "a3"], False), ("титан", ["a3"], False), ("титан", [], True),
])
def test_habit_never_argues_with_the_shade_in_the_letter(target, shades, fits):
    from app.services.material_suggest import shades_in

    real = [shades_in(f"mono {s}")[0] for s in shades]
    assert rules.empty_fits(target, real) is fits


def test_letters_with_other_shades_do_not_support_an_empty_rule(db):
    rows = [{**_row(i, i, system="", chosen="титан"), "shades": ["A3"]} for i in range(10)]
    assert learn.mine(db, rows=rows)["candidates"] == []
