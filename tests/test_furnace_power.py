"""Піч без зв'язку під час спікання (`app/services/furnace_power.py`, власник 20.11.26).

Піч на батареї резервного живлення; батарея сідає посеред спікання — табло
мовчить. Правило: останній кадр показував RUN і піч мовчить ≥ 1 хв →
повідомлення, що висить до «Зрозуміло». Перехід, а не стан: WAIT і коротка
пауза мережі повідомлення не дають, повторні тіки не плодять дублів.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pytest

from app.models import FurnaceOutage, FurnaceReading, User
from app.services import furnace_power as fp
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

T0 = datetime(2026, 11, 20, 3, 0, 0)
KEY = "192.168.1.16"


@dataclass
class _Target:
    name: str = "Бочка"


@dataclass
class _State:
    attempted_at: Optional[datetime]
    captured_at: Optional[datetime] = None
    error: Optional[str] = None
    status: str = "?"
    temp_c: Optional[int] = None
    target: _Target = field(default_factory=_Target)


def _ok(at: datetime, status: str = "RUN", temp: Optional[int] = 1450) -> dict:
    return {KEY: _State(attempted_at=at, captured_at=at, status=status, temp_c=temp)}


def _silent(at: datetime) -> dict:
    return {KEY: _State(attempted_at=at, error="Кадр не знято: timed out")}


@pytest.fixture(autouse=True)
def _fresh_memory():
    fp.reset_memory()
    yield
    fp.reset_memory()


def _outages(db) -> list[FurnaceOutage]:
    return db.query(FurnaceOutage).order_by(FurnaceOutage.id).all()


def test_run_then_silence_for_a_minute_gives_one_notice_until_ack(db_session):
    fp.tick(db_session, _ok(T0), now=T0)
    # Пауза мережі коротша за хвилину — нічого.
    for s in (6, 30, 54):
        fp.tick(db_session, _silent(T0 + timedelta(seconds=s)), now=T0 + timedelta(seconds=s))
    assert _outages(db_session) == []
    # Хвилина мовчання — одне повідомлення, повторні тіки дублів не дають.
    for s in (60, 66, 600):
        fp.tick(db_session, _silent(T0 + timedelta(seconds=s)), now=T0 + timedelta(seconds=s))
    rows = _outages(db_session)
    assert len(rows) == 1
    assert rows[0].started_at == T0 and rows[0].recovered_at is None and rows[0].last_temp_c == 1450
    assert rows[0].furnace_name == "Бочка"
    # Світло дали — час повернення, але повідомлення лишається до «Зрозуміло».
    back = T0 + timedelta(minutes=40)
    fp.tick(db_session, _ok(back, status="WAIT", temp=300), now=back)
    row = _outages(db_session)[0]
    assert row.recovered_at == back and row.acked_at is None
    [notice] = fp.notices(db_session)
    assert not notice.is_open and notice.minutes(back) == 40
    assert fp.acknowledge(db_session, None, notice.id, now=back)
    assert fp.notices(db_session) == []


def test_silence_in_wait_or_unknown_furnace_is_not_a_notice(db_session):
    fp.tick(db_session, _ok(T0, status="WAIT"), now=T0)
    later = T0 + timedelta(minutes=10)
    fp.tick(db_session, _silent(later), now=later)
    # Піч, про яку з самого старту нічого не відомо, — теж мовчання без вироку.
    other = {"10.0.0.9": _State(attempted_at=later, error="timeout")}
    fp.tick(db_session, other, now=later)
    assert _outages(db_session) == []


def test_unsure_status_keeps_the_last_known_one(db_session):
    """Кадр є, але сигнали статусу розійшлись («?») — піч жива, RUN лишається."""
    fp.tick(db_session, _ok(T0), now=T0)
    t1 = T0 + timedelta(minutes=5)
    fp.tick(db_session, _ok(t1, status="?", temp=1500), now=t1)
    t2 = t1 + timedelta(minutes=2)
    fp.tick(db_session, _silent(t2), now=t2)
    [row] = _outages(db_session)
    assert row.started_at == t1 and row.last_temp_c == 1500


def test_after_restart_the_last_fresh_reading_from_the_base_counts(db_session):
    db_session.add(FurnaceReading(host=KEY, captured_at=T0, status="RUN", temp_c=1200))
    db_session.commit()
    t = T0 + timedelta(minutes=3)
    fp.tick(db_session, _silent(t), now=t)
    [row] = _outages(db_session)
    assert row.started_at == T0 and row.last_temp_c == 1200


def test_after_long_restart_the_old_reading_is_not_a_verdict(db_session):
    db_session.add(FurnaceReading(host=KEY, captured_at=T0, status="RUN", temp_c=1200))
    db_session.commit()
    t = T0 + timedelta(hours=2)
    fp.tick(db_session, _silent(t), now=t)
    assert _outages(db_session) == []


def test_stale_state_is_ignored(db_session):
    fp.tick(db_session, _ok(T0), now=T0)
    t = T0 + timedelta(minutes=10)
    stale = {KEY: _State(attempted_at=T0 + timedelta(seconds=6), error="timeout")}
    fp.tick(db_session, stale, now=t)
    assert _outages(db_session) == []


def test_tick_never_raises(db_session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("база")

    monkeypatch.setattr(fp, "_note_ok", boom)
    fp.tick(db_session, _ok(T0), now=T0)  # не кидає


def test_access_decides_who_sees_it(db_session):
    db_session.add(FurnaceOutage(furnace_key=KEY, furnace_name="Бочка", started_at=T0, detected_at=T0))
    db_session.commit()
    user = User(username="op", password_hash="x")
    assert fp.notices_for(db_session, user) == []
    user.furnace_power_alert = True
    assert len(fp.notices_for(db_session, user)) == 1
    user.furnace_power_access = "deny"
    assert fp.notices_for(db_session, user) == []
    user.furnace_power_alert = False
    user.furnace_power_access = "force"
    assert len(fp.notices_for(db_session, user)) == 1


def test_replay_finds_run_outages_and_skips_wait(db_session):
    rows = [
        FurnaceReading(host=KEY, captured_at=T0, status="RUN", temp_c=1300),
        FurnaceReading(host=KEY, captured_at=T0 + timedelta(seconds=6), status="", error="timeout"),
        FurnaceReading(host=KEY, captured_at=T0 + timedelta(minutes=30), status="WAIT", temp_c=400),
        FurnaceReading(host=KEY, captured_at=T0 + timedelta(hours=1), status="WAIT", temp_c=100),
        FurnaceReading(host=KEY, captured_at=T0 + timedelta(hours=1, seconds=6), status="", error="timeout"),
        FurnaceReading(host=KEY, captured_at=T0 + timedelta(hours=9), status="WAIT", temp_c=20),
    ]
    db_session.add_all(rows)
    db_session.commit()
    out = fp.replay(db_session, days=3, now=T0 + timedelta(hours=10))
    assert out["повідомлень_було_б"] == 1
    assert out["обриви_під_час_спікання"][0]["хв"] == 30
    assert out["обривів_поза_спіканням"] == 1


def _make_outage(factory) -> int:
    with factory() as db:
        row = FurnaceOutage(furnace_key=KEY, furnace_name="Бочка", started_at=T0, detected_at=T0,
                            last_temp_c=1450)
        db.add(row)
        db.commit()
        return row.id


def test_slot_ack_and_switches_over_http(app_db):  # noqa: F811
    app, factory = app_db
    outage_id = _make_outage(factory)
    operator = MiniClient(app)
    operator.login(*OPERATOR)

    # Вимкнено (за замовчуванням) — порожня смуга, кнопка не діє.
    status, _, html = operator.get("/furnace-power/slot")
    assert status == 200 and "fpower-strip" not in html
    assert operator.post("/furnace-power/ack", {"outage_id": str(outage_id)})[0] == 403

    assert operator.post("/account/furnace-power", {"alert": "on"})[0] == 204
    _, _, html = operator.get("/furnace-power/slot")
    assert "Бочка не відповідає" in html and "1450 °C" in html and "Зрозуміло" in html
    # Будь-яка сторінка під сесією несе слот.
    _, _, page = operator.get("/account")
    assert 'id="fpower-slot"' in page and "Піч без зв'язку під час спікання" in page

    status, _, html = operator.post("/furnace-power/ack", {"outage_id": str(outage_id)})
    assert status == 200 and "fpower-strip" not in html
    with factory() as db:
        row = db.get(FurnaceOutage, outage_id)
        user = db.query(User).filter_by(username=OPERATOR[0]).one()
        assert row.acked_at is not None and row.acked_by_id == user.id

    assert operator.post("/account/furnace-power", {"alert": "maybe"})[0] == 422


def test_admin_access_for_furnace_power(app_db):  # noqa: F811
    app, factory = app_db
    _make_outage(factory)
    with factory() as db:
        operator_id = db.query(User).filter_by(username=OPERATOR[0]).one().id
    admin = MiniClient(app)
    admin.login(*ADMIN)
    url = f"/settings/users/{operator_id}/feature-access"
    _, _, page = admin.get("/settings")
    assert 'name="feature" value="furnace_power"' in page and "Піч без зв&#39;язку" in page

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert admin.post(url, {"feature": "furnace_power", "access": "force"})[0] == 303
    _, _, html = operator.get("/furnace-power/slot")
    assert "Бочка не відповідає" in html
    assert operator.post("/account/furnace-power", {"alert": "off"})[0] == 403

    assert admin.post(url, {"feature": "furnace_power", "access": "deny"})[0] == 303
    _, _, html = operator.get("/furnace-power/slot")
    assert "fpower-strip" not in html
    assert operator.post("/account/furnace-power", {"alert": "on"})[0] == 403
    _, _, account = operator.get("/account")
    assert "Адміністратор вимкнув для вас це повідомлення" in account
    # Доступ до сторожа матеріалу від цього не змінився.
    with factory() as db:
        user = db.get(User, operator_id)
        assert user.material_guard_access == "" and user.furnace_power_access == "deny"
