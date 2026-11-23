"""Журнал підбору (власник 22.11.26): що запропонувала картка листа при
прийнятті і що вибрав оператор; фактичний диск дописує добовий прохід."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import MachineReading, Order, SelectionRecord
from app.services import selection_journal as sj
from tests.test_mail_transaction_safety import _database, _letter, _user, _wire


@pytest.fixture(autouse=True)
def _clean_shown():
    sj.forget_shown()
    yield
    sj.forget_shown()


def _pick(name, height, free, started=True, fit=True):
    return SimpleNamespace(name=name, height=height, free_pct=free, started=started, fit=fit, on_machine=False, tight=False)


def _hint(best=None, alts=(), heights=(16,), thickness=12.5):
    return SimpleNamespace(thickness=thickness, heights=list(heights), best=best, alts=list(alts),
                           new_disc="новий диск 16 мм Monolith A3", note="", rejected=0)


def test_accept_records_card_choice_and_shown_hint(tmp_path, monkeypatch):
    from app.services import mail_accept as svc

    engine = _database()
    _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _ = _letter(db, tmp_path / "spool" / "u1")
        hint = _hint(_pick("zr16_16-Monolith-A3-x37", 16, 64), [_pick("zr16_16-Monolith-A3-x40", 16, 100, started=False)])
        sj.remember_shown([email.id], "mono a3", hint)
        result = svc.accept_letter(db, user, email, client_name="Люмі-Дент", material_color="mono a3",
                                   quantity="2")
        assert result.ok, result.error
        rec = db.query(SelectionRecord).one()
    assert rec.mode == "card" and rec.email_id == email.id and rec.order_id == result.order.id
    assert rec.user_id == user.id and rec.chosen_material == "mono a3" and rec.quantity == "2"
    shown = json.loads(rec.hint_json)
    assert shown["best"]["disc"] == "zr16_16-Monolith-A3-x37" and shown["best"]["free_pct"] == 64
    assert [p["disc"] for p in shown["alts"]] == ["zr16_16-Monolith-A3-x40"]
    assert rec.hint_material == "mono a3"


def test_accept_without_shown_hint_still_records_material(tmp_path, monkeypatch):
    from app.services import mail_accept as svc

    engine = _database()
    _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _ = _letter(db, tmp_path / "spool" / "u1")
        result = svc.accept_letter(db, user, email, client_name="Люмі-Дент", material_color="mono a3",
                                   selection_mode="conveyor")
        assert result.ok, result.error
        rec = db.query(SelectionRecord).one()
    assert rec.mode == "conveyor" and rec.hint_json is None and rec.chosen_material == "mono a3"


def test_journal_failure_never_breaks_the_accept(tmp_path, monkeypatch):
    from app.services import mail_accept as svc
    from app.services import mail_letter_signals

    def boom(*_a, **_k):
        raise RuntimeError("журнал зламався")

    monkeypatch.setattr(mail_letter_signals, "card_material", boom)
    engine = _database()
    _wire(monkeypatch, tmp_path)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _ = _letter(db, tmp_path / "spool" / "u1")
        result = svc.accept_letter(db, user, email, client_name="Люмі-Дент", material_color="mono a3")
        assert result.ok, result.error
        assert db.query(SelectionRecord).count() == 0
        assert db.get(Order, result.order.id) is not None


def _record(db, *, order_id, created, hint, chosen="mono a3", suggested="mono a3"):
    rec = SelectionRecord(created_at=created, order_id=order_id, email_id=1, chosen_material=chosen,
                          suggested_material=suggested,
                          hint_json=json.dumps(sj.hint_summary(hint)) if hint else None)
    db.add(rec)
    db.commit()
    return rec


def test_resolve_actual_takes_disc_from_machine_program_and_report_compares():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        accepted = datetime(2026, 11, 20, 10)
        first = Order(source="email", status="прораховано", sum3d_id="2026-11-20_10-17-22")
        other = Order(source="email", status="прораховано", sum3d_id="11-00-32")
        none = Order(source="email", status="прораховано")
        db.add_all([first, other, none])
        db.commit()
        hint = _hint(_pick("zr16_16-Monolith-A3-x37", 16, 64), [_pick("zr16_16-Monolith-A3-x40", 16, 100)])
        r1 = _record(db, order_id=first.id, created=accepted, hint=hint)
        r2 = _record(db, order_id=other.id, created=accepted, hint=hint, chosen="emo a3", suggested="mono a3")
        r3 = _record(db, order_id=none.id, created=accepted, hint=None)
        db.add_all([
            # Той самий Sum3D-хвіст місяць тому — чужа робота, поза вікном.
            MachineReading(host="h0", captured_at=accepted - timedelta(days=20), sum3d_id="10-17-22",
                           iso_name="1_14-Monolith-A3-x99_2026-10-31_10-17-22.iso"),
            MachineReading(host="h1", captured_at=accepted + timedelta(hours=5), sum3d_id="10-17-22",
                           iso_name="1_16-Monolith-A3-x37_2026-11-20_10-17-22.iso"),
            MachineReading(host="h2", captured_at=accepted + timedelta(hours=6), sum3d_id="11-00-32",
                           iso_name="1_18-Emotions-A3-x05_2026-11-20_11-00-32.iso"),
        ])
        db.commit()

        assert sj.resolve_actual(db, now=accepted + timedelta(days=1)) == 2
        db.refresh(r1), db.refresh(r2), db.refresh(r3)
        assert (r1.actual_disc, r1.actual_height) == ("16-monolith-a3-x37", 16)
        assert (r2.actual_disc, r2.actual_height) == ("18-emotions-a3-x05", 18)
        assert r3.actual_at is None, "без Sum3D — ще чекаємо"
        # Минув строк — позначка й кінець пошуку.
        sj.resolve_actual(db, now=accepted + timedelta(days=sj.GIVE_UP_DAYS + 1))
        db.refresh(r3)
        assert r3.actual_note == "без Sum3D" and r3.actual_at is not None

        report = sj.report(db, days=7, now=accepted + timedelta(days=1))
    assert report["прийнять"] == 3
    assert report["матеріал"] == {"збіг": 2, "оператор змінив": 1}
    assert report["пари_картка_оператор"] == {"mono a3 → emo a3": 1}
    assert report["диск"]["взяли перший кандидат"] == 1
    assert report["диск"]["взяли диск поза підказкою"] == 1
    assert report["диск"]["підказку не показано"] == 1
    assert report["висота"] == {"висота як у підказці": 1, "висота інша": 1}
    assert {e["робота"] for e in report["приклади"]} == {other.id}
    assert all("клієнт" not in key for e in report["приклади"] for key in e), "імен клієнтів не віддаємо"
