"""Картка клієнта: що витягується з листів і що людина про це вирішила (07.11.26)."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.db import Base
from app.models import Client, ClientFactDecision, ClientSenderMemory, EmailMessage, Order, OrderEmail, User
from app.services import client_insights as ci
from tests.asgi_client import MiniClient


@pytest.fixture(autouse=True)
def _fresh_cache():
    ci.reset_cache_for_tests()
    ci.data.clear_cache()
    yield
    ci.reset_cache_for_tests()
    ci.data.clear_cache()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _letter(db, uid="1", body="", subject="", address="lab@example.com", name="Олена", at=datetime(2026, 11, 3, 11, 0), **kw):
    letter = EmailMessage(uid=uid, from_address=address, from_name=name, subject=subject, body_text=body, received_at=at, **kw)
    db.add(letter)
    db.flush()
    return letter


def _work(db, letter=None, name="Лабораторія", tab="03.11.26", qty="5", sum3d="12-00-00", material="mono a3", **kw):
    order = Order(source="email", sheet_tab=tab, quantity=qty, sum3d_id=sum3d, client_name=name, material_color=material,
                  source_email_id=letter.id if letter else None, **kw)
    db.add(order)
    db.flush()
    return order


def _client(db, name="Лабораторія"):
    client = Client(canonical_name=name)
    db.add(client)
    db.flush()
    return client


def phones(text):
    return [p for p, _, _ in ci.find_phones(text)]


def ships(text):
    return [s for s, _, _ in ci.find_shipping(text)]


# ── Телефони ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["+380 67 123 45 67", "067-123-45-67", "(067) 123 45 67", "0671234567", "+38 067 123 4567", "тел.067 123-45-67,"])
def test_phone_shapes_become_one_value(raw):
    assert phones(raw) == ["+38 067 123 45 67"]


@pytest.mark.parametrize("raw", ["2026-07-21_00016-007", "12-01-45", "24122", "2026-10-22_00002-001-14-crown_cad.stl", "наряд 0012345678", "вкладка 22.07.26"])
def test_ids_from_the_shop_are_not_phones(raw):
    assert phones(raw) == []


def test_quote_of_a_reply_does_not_give_our_own_number(db):
    letter = _letter(db, body="Дякую, мій 067 111 22 33\n\n-----Original Message-----\nЛаба 050 999 88 77")
    facts, _ = ci.extract_facts([ci.Letter(letter.id, letter.from_address, letter.from_name, letter.subject, letter.body_text, letter.received_at)])
    assert [f.value for f in facts if f.kind == "phone"] == ["+38 067 111 22 33"]


def test_forwarded_letter_keeps_the_client_text_below_the_marker(db):
    letter = _letter(db, subject="Fwd: робота", body="-----Original Message-----\nОт: client@example.com\nмій 067 111 22 33")
    facts, _ = ci.extract_facts([ci.Letter(letter.id, letter.from_address, letter.from_name, letter.subject, letter.body_text, letter.received_at)])
    values = {(f.kind, f.value) for f in facts}
    assert ("phone", "+38 067 111 22 33") in values
    assert ("sender", "Олена") not in values, "у пересланому листі From це той, хто переслав"


def test_number_from_three_senders_is_not_the_client(db):
    for i in range(3):
        _letter(db, uid=str(i), address=f"a{i}@example.com", body="підпис 050 999 88 77")
    db.commit()
    assert "+38 050 999 88 77" in ci.shared_phones(db)


# ── Доставка ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Відправте на НП Дніпро, відд. 14", "Нова пошта, Дніпро, відділення 14"),
        ("нова пошта м. Львів відділення №8", "Нова пошта, Львів, відділення 8"),
        ("Зроби А2 на 208 відділення", "Нова пошта, відділення 208"),
        ("Новая почта Киев 201", "Нова пошта, Київ, відділення 201"),
        ("НП Харків", "Нова пошта, Харків"),
    ],
)
def test_shipping_shapes(raw, expected):
    assert ships(raw) == [expected]


@pytest.mark.parametrize("raw", ["стоматологічне відділення лікарні", "НПЗ завод", "1 од. на абатмент 14"])
def test_words_that_are_not_shipping(raw):
    assert ships(raw) == []


def test_surname_next_to_np_never_becomes_a_city():
    """Місто лише з переліку: прізвище поруч із «НП» може бути прізвищем пацієнта."""
    assert ships("НП Петренко 14") == ["Нова пошта, відділення 14"]


def test_city_is_taken_only_from_the_same_line():
    assert ships("м. Київ, вул. Хрещатик\nНП відділення 5") == ["Нова пошта, відділення 5"]


def test_pickup_and_ukrposhta():
    assert ships("заберемо самі") == ["Самовивіз або кур'єр клієнта"]
    assert ships("Укрпошта Одеса") == ["Укрпошта, Одеса"]


# ── Чиї це листи ─────────────────────────────────────────────────────────────
def test_letters_of_a_client_come_from_his_works_and_merged_letters(db):
    first = _letter(db, uid="1")
    second = _letter(db, uid="2")
    _letter(db, uid="3", address="stranger@example.com")
    order = _work(db, first)
    db.add(OrderEmail(order_id=order.id, email_message_id=second.id))
    client = _client(db)
    db.commit()
    assert {x.id for x in ci.client_letters(db, client, [order])} == {first.id, second.id}


def test_unaccepted_letter_of_a_remembered_sender_counts(db):
    letter = _letter(db, uid="1", address="lab@example.com")
    db.add(ClientSenderMemory(sender_key="lab@example.com", client_name="Лабораторія", last_seen_at=datetime(2026, 11, 1)))
    client = _client(db)
    db.commit()
    assert [x.id for x in ci.client_letters(db, client, [])] == [letter.id]


def test_shared_mailbox_does_not_mix_two_clients(db):
    """Одна адреса пересилає листи різних клієнтів: чужий переслатий лист не наш."""
    _letter(db, uid="1", address="box@example.com", subject="Fwd: x", body="From: other@example.com\nчуже")
    mine = _letter(db, uid="2", address="box@example.com", subject="Fwd: y", body="From: mine@example.com\nнаше")
    db.add(ClientSenderMemory(sender_key="box@example.com|mine@example.com", client_name="Лабораторія", last_seen_at=datetime(2026, 11, 1)))
    client = _client(db)
    db.commit()
    assert [x.id for x in ci.client_letters(db, client, [])] == [mine.id]


def test_filtered_letters_are_not_counted(db):
    letter = _letter(db, uid="1", filter_category="3D-друк")
    order = _work(db, letter)
    client = _client(db)
    db.commit()
    assert ci.client_letters(db, client, [order]) == []


# ── Ритм і обсяг ─────────────────────────────────────────────────────────────
def _at(day, hour=11):
    return datetime(2026, 11, day, hour)


def _plain(at, i=0):
    return ci.Letter(i, "a@example.com", "A", "", "", at)


def test_rhythm_says_quiet_only_with_enough_history():
    few = [_plain(_at(d)) for d in (1, 2)]
    assert ci.rhythm(few, date(2026, 11, 30))["enough"] is False
    many = [_plain(_at(d)) for d in (1, 3, 5, 7, 9, 11)]
    usual = ci.rhythm(many, date(2026, 11, 12))
    assert usual["median"] == 2 and usual["quiet"] is False
    silent = ci.rhythm(many, date(2026, 11, 25))
    assert silent["since"] == 14 and silent["quiet"] is True


def test_rhythm_heat_counts_day_and_hour():
    result = ci.rhythm([_plain(datetime(2026, 11, 3, 23)), _plain(datetime(2026, 11, 3, 23))], date(2026, 11, 7))
    assert result["cells"][1][23]["n"] == 2  # вівторок, 23:00
    assert result["top_days"] == ["вт"]


def test_empty_rhythm_does_not_crash():
    result = ci.rhythm([], date(2026, 11, 7))
    assert result["total"] == 0 and result["top_hours"] is None and result["since"] is None


def test_volume_counts_only_done_works(db):
    done = _work(db, qty="5")
    no_sum3d = _work(db, qty="7", sum3d=None)
    ghost = _work(db, qty="37875")
    db.commit()
    result = ci.volume(db, [done, no_sum3d, ghost], date(2026, 11, 7))
    assert result["units"] == 5 and result["works"] == 1
    assert result["materials"][0]["label"] == "mono a3"
    assert result["weeks"][-1]["units"] == 5


# ── Рішення людини ───────────────────────────────────────────────────────────
def test_decision_is_kept_after_a_new_letter_and_can_be_cleared(db):
    client = _client(db)
    ci.set_decision(db, client, "phone", "+38 067 111 22 33", ci.STATE_HIDE, "op")
    db.commit()
    fact = ci.Fact("phone", "+38 067 111 22 33", "+38 067 111 22 33", count=9)
    grouped = ci.apply_decisions(db, client, [fact])
    assert grouped["phone"][0].state == ci.STATE_HIDE

    ci.set_decision(db, client, "phone", "+38 067 111 22 33", ci.STATE_PIN, "op")
    db.commit()
    assert db.query(ClientFactDecision).count() == 1, "рішення на значення одне"
    ci.set_decision(db, client, "phone", "+38 067 111 22 33", None, "op")
    db.commit()
    assert db.query(ClientFactDecision).count() == 0


def test_pinned_beats_frequent_and_hidden_is_not_offered_to_logistics(db):
    client = _client(db)
    ci.set_decision(db, client, "ship", "нова пошта, львів", ci.STATE_PIN, "op")
    ci.set_decision(db, client, "phone", "+38 067 111 22 33", ci.STATE_HIDE, "op")
    db.commit()
    facts = [
        ci.Fact("ship", "нова пошта, київ", "Нова пошта, Київ", count=9),
        ci.Fact("ship", "нова пошта, львів", "Нова пошта, Львів", count=1),
        ci.Fact("phone", "+38 067 111 22 33", "+38 067 111 22 33", count=9),
    ]
    block = ci._ship_block(client, ci.apply_decisions(db, client, facts))
    assert block["where"] == "Нова пошта, Львів"
    assert block["phone"] is None


def test_phone_typed_by_hand_wins(db):
    client = _client(db)
    client.phone = "+380 50 000 00 00"
    facts = [ci.Fact("phone", "+38 067 111 22 33", "+38 067 111 22 33", count=9)]
    assert ci._ship_block(client, ci.apply_decisions(db, client, facts))["phone"] == "+380 50 000 00 00"


# ── Докази й замір ───────────────────────────────────────────────────────────
def test_proof_is_a_short_fragment_with_the_value_marked():
    body = "пацієнт Іваненко " + "x" * 80 + " мій 067 111 22 33 дякую"
    found = ci.proof([ci.Letter(1, "a@example.com", "A", "", body, _at(3))], "phone", "+38 067 111 22 33")
    assert found[0]["match"] == "067 111 22 33"
    assert found[0]["before"].endswith("мій "), "пробіл перед значенням не зникає"
    assert "Іваненко" not in found[0]["before"], "уривок не тягне весь лист"


def test_audit_masks_phones_and_has_no_letter_text(db):
    letter = _letter(db, body="пацієнт Іваненко, мій 067 111 22 33, НП Дніпро 14")
    _work(db, letter)
    _client(db)
    db.commit()
    result = ci.audit(db, 10)
    text = str(result)
    assert result["із_телефоном"] == 1 and result["із_доставкою"] == 1
    assert "Іваненко" not in text and "111 22 33" not in text and "***" in text


# ── Екран ────────────────────────────────────────────────────────────────────
@pytest.fixture
def app_db(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    import app.web as web
    from app import license as license_module
    from app.routers import deps
    from app.settings_store import set_setting

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    def factory():
        return Session(engine, expire_on_commit=False)

    monkeypatch.setattr(deps, "SessionLocal", factory)
    monkeypatch.setattr(web, "SessionLocal", factory, raising=False)
    private = Ed25519PrivateKey.generate()
    monkeypatch.setattr(license_module, "_PUBLIC_KEY_BYTES", private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    with factory() as db:
        set_setting(db, "license_key", license_module.encode_license_key(
            {"machine_id": license_module.get_machine_id(), "customer": "tests", "issued_at": "2026-01-01T00:00:00", "expires_at": "2099-01-01T00:00:00"}, private))
        db.add(User(username="cardop", password_hash=hash_password("Card-Op-1!x"), full_name="cardop", role="оператор"))
        letter = _letter(db, body="Відправте НП Дніпро, відд. 14, мій 067 111 22 33 на швидку <script>")
        _work(db, letter)
        client = _client(db)
        db.commit()
        client_id = client.id
    return web.app, factory, client_id


def _logged(app):
    http = MiniClient(app)
    status, _, _ = http.login("cardop", "Card-Op-1!x")
    assert status in (200, 302, 303)
    return http


def test_card_renders_facts_rhythm_and_volume(app_db):
    app, _, client_id = app_db
    status, _, html = _logged(app).get(f"/clients/{client_id}/pane")
    assert status == 200
    for piece in ("Дані для відправки", "+38 067 111 22 33", "Нова пошта, Дніпро, відділення 14", "на швидку", "cx-heat", "cx-bars", 'id="client-contacts"'):
        assert piece in html, piece


def test_old_card_controls_are_still_there(app_db):
    app, _, client_id = app_db
    _, _, html = _logged(app).get(f"/clients/{client_id}/pane")
    for piece in ('name="export_folder_name"', 'id="client-pane"', 'name="notes"', 'name="phone"'):
        assert piece in html, piece


def test_hide_swaps_only_the_left_column_and_can_be_returned(app_db):
    app, factory, client_id = app_db
    http = _logged(app)
    form = {"kind": "phone", "key": "+38 067 111 22 33", "action": "hide"}
    status, _, html = http.post(f"/clients/{client_id}/facts", data=form, headers={"HX-Request": "true"})
    assert status == 200
    assert 'id="client-facts"' in html and "cx-heat" not in html
    assert "Прибрано: 1" in html
    status, _, html = http.post(f"/clients/{client_id}/facts", data={**form, "action": "clear"}, headers={"HX-Request": "true"})
    assert "Прибрано" not in html and "закріплено" not in html


def test_bad_fact_action_is_refused(app_db):
    app, _, client_id = app_db
    status, _, _ = _logged(app).post(f"/clients/{client_id}/facts", data={"kind": "phone", "key": "x", "action": "drop"}, headers={"HX-Request": "true"})
    assert status == 400
    status, _, _ = _logged(app).post(f"/clients/{client_id}/facts", data={"kind": "pacient", "key": "x", "action": "pin"}, headers={"HX-Request": "true"})
    assert status == 400


def test_proof_route_escapes_the_letter_text(app_db):
    app, _, client_id = app_db
    status, _, html = _logged(app).get(f"/clients/{client_id}/fact-proof?kind=phone&key=%2B38%20067%20111%2022%2033")
    assert status == 200 and "<mark>067 111 22 33</mark>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_slot_route_lists_letters_of_the_hour(app_db):
    app, _, client_id = app_db
    status, _, html = _logged(app).get(f"/clients/{client_id}/slot?d=1&h=11")
    assert status == 200 and "03.11.26, 11:00" in html
    _, _, empty = _logged(app).get(f"/clients/{client_id}/slot?d=6&h=3")
    assert "листів не було" in empty


def test_new_routes_need_login(app_db):
    app, _, client_id = app_db
    status, _, _ = MiniClient(app).get(f"/clients/{client_id}/slot?d=1&h=11")
    assert status in (302, 303)
