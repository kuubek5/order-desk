"""Прогноз: модель на чистих числах, запис наперед, журнал і справжні роути."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ForecastSnapshot, Order, StatsAnomalyDay
from app.services import stats_data as data
from app.services import stats_forecast as fc
from tests.test_stats import ADMIN, OPERATOR, _client, app_db  # noqa: F401 — фікстура

MONDAY = date(2026, 11, 9)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    data.clear_cache()
    yield engine
    data.clear_cache()


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


def _weekly(values_by_week: list[int], weekday: int = 0, end: date = MONDAY) -> dict[date, int]:
    """Дані одного дня тижня: перше значення — найстаріший тиждень."""
    last = end - timedelta(days=(end.weekday() - weekday) % 7 or 7)
    count = len(values_by_week)
    return {last - timedelta(weeks=count - 1 - i): v for i, v in enumerate(values_by_week)}


def _seed_weeks(db, weeks: int, units: int = 10, end: date = MONDAY) -> None:
    """Кожен робочий день має лабораторну роботу з унікальним Sum3D."""
    index = 0
    for offset in range(1, weeks * 7 + 1):
        day = end - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        index += 1
        db.add(
            Order(
                source="lab", sheet_tab=day.strftime("%d.%m.%y"), quantity=str(units), work_order_no=str(index),
                sum3d_id=f"{index % 24:02d}-{index % 60:02d}-{index % 59:02d}", material_color="моно а3",
            )
        )
    db.commit()


# ── Модель ───────────────────────────────────────────────────────────────────
def test_too_little_history_gives_no_number():
    assert fc.predict(_weekly([10] * 5), MONDAY, MONDAY, set()) is None
    assert fc.predict(_weekly([10] * 6), MONDAY, MONDAY, set()) is not None


def test_flat_history_predicts_itself():
    series = {}
    for weekday in range(5):
        series.update(_weekly([100] * 10, weekday))
    assert fc.predict(series, MONDAY, MONDAY, set()) == pytest.approx(100.0)


def test_extremes_are_trimmed():
    assert fc.predict(_weekly([100, 100, 100, 100, 100, 100, 100, 1000]), MONDAY, MONDAY, set()) == pytest.approx(100.0, rel=0.35)


def test_trend_is_limited():
    """Аврал останніх двох тижнів не мусить множити прогноз утричі."""
    series = {}
    for weekday in range(5):
        series.update(_weekly([100] * 8 + [1000, 1000], weekday))
    value = fc.predict(series, MONDAY, MONDAY, set())
    assert value is not None and value <= 1000 * fc.TREND_LIMITS[1]


def test_anomaly_day_does_not_teach_the_model():
    series = _weekly([100, 100, 100, 100, 100, 100, 100, 5])
    flagged = {max(series)}
    with_day = fc.predict(series, MONDAY, MONDAY, set())
    without = fc.predict(series, MONDAY, MONDAY, flagged)
    assert without == pytest.approx(100.0)
    assert with_day != without


def test_future_days_do_not_leak_into_the_past():
    """Перевірка на минулому не сміє знати про день, який перевіряє."""
    series = _weekly([100] * 8)
    last = max(series)
    series_with_spike = {**series, last: 9999}
    assert fc.predict(series, last, last, set()) == fc.predict(series_with_spike, last, last, set())


def test_naive_is_the_same_day_last_week():
    series = _weekly([70, 80])
    target = max(series) + timedelta(days=7)
    assert fc.naive(series, target, set()) == 80
    assert fc.naive(series, target, {max(series)}) is None


def test_score_skips_anomalies_and_zero_days():
    rows = [
        {"forecast": 110.0, "actual": 100, "naive": 200, "anomaly": False},
        {"forecast": 500.0, "actual": 100, "naive": 200, "anomaly": True},
        {"forecast": 50.0, "actual": 0, "naive": 0, "anomaly": False},
    ]
    score = fc._score(rows, "forecast")
    assert score["n"] == 1 and score["mape"] == pytest.approx(10.0) and score["within"] == 100.0


def test_targets_skip_weekends():
    assert fc.targets_from(date(2026, 11, 5), 3) == [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]


# ── Запис наперед ────────────────────────────────────────────────────────────
def test_snapshot_is_written_once_a_day(db):
    _seed_weeks(db, 12)
    written = fc.write_snapshot(db, MONDAY)
    assert written > 0
    assert fc.write_snapshot(db, MONDAY) == 0
    rows = db.scalars(select(ForecastSnapshot)).all()
    assert len(rows) == written
    assert {row.series for row in rows} >= {"total", "lab"}
    assert {row.target_day for row in rows} == set(fc.targets_from(MONDAY))


def test_short_history_writes_nothing(db):
    _seed_weeks(db, 3)
    assert fc.write_snapshot(db, MONDAY) == 0


def test_friday_tab_snapshot_on_saturday_targets_friday_first(db):
    """У суботу вкладка ще п'ятнична: фактичний день прогнозу теж п'ятниця."""
    _seed_weeks(db, 12)
    fc.write_snapshot(db, date(2026, 11, 7))
    days = {row.target_day for row in db.scalars(select(ForecastSnapshot))}
    assert date(2026, 11, 6) in days


def test_written_value_is_not_rewritten(db):
    _seed_weeks(db, 12)
    fc.write_snapshot(db, MONDAY)
    first = db.scalar(select(ForecastSnapshot).where(ForecastSnapshot.series == "total", ForecastSnapshot.target_day == MONDAY))
    before = first.value
    db.add(Order(source="lab", sheet_tab="02.11.26", quantity="199", work_order_no="x", sum3d_id="11-11-11", material_color="моно а3"))
    db.commit()
    data.clear_cache()
    fc.write_snapshot(db, MONDAY)
    assert db.get(ForecastSnapshot, first.id).value == before


def test_journal_uses_what_was_written_not_a_recalculation(db):
    _seed_weeks(db, 12)
    day = MONDAY - timedelta(days=7)
    db.add(ForecastSnapshot(made_on=day, target_day=day, series="total", value=5.0, model_version="t"))
    db.commit()
    built = fc.build(db, MONDAY)
    row = next(r for r in built["rows"] if r["day"] == day)
    assert row["written"] is True and row["forecast"] == "5"
    assert built["journal"]["n"] == 1


def test_build_says_when_there_is_too_little(db):
    _seed_weeks(db, 3)
    built = fc.build(db, MONDAY)
    assert built["enough"] is False and built["weeks_known"] == 3


def test_week_brief_adds_fact_to_forecast(db):
    _seed_weeks(db, 12)
    db.add(Order(source="lab", sheet_tab=MONDAY.strftime("%d.%m.%y"), quantity="7", work_order_no="m", sum3d_id="10-11-12", material_color="моно а3"))
    db.commit()
    assert fc.week_brief(db, MONDAY)["fact"] == "0", "сьогоднішній день ще прогноз, а не факт"
    data.clear_cache()
    brief = fc.week_brief(db, MONDAY + timedelta(days=1))
    assert brief["fact"] == "7"
    assert int(brief["total"].replace(" ", "").replace(" ", "")) > 7


# ── Роути ────────────────────────────────────────────────────────────────────
QUERY = {"tab": "forecast"}


def test_forecast_tab_renders_empty(app_db):  # noqa: F811
    status, _, body = _client(ADMIN).get("/stats?" + urlencode(QUERY))
    assert status == 200 and "Замало даних" in body


def test_only_admin_can_mark_a_day(app_db, engine):  # noqa: F811
    status, _, _ = _client(OPERATOR).post("/stats/anomaly?" + urlencode(QUERY), {"day": "2026-11-03"})
    assert status == 403
    with Session(engine) as session:
        assert session.scalar(select(StatsAnomalyDay)) is None


def test_admin_marks_and_unmarks_a_day(app_db, engine):  # noqa: F811
    client = _client(ADMIN)
    status, _, _ = client.post("/stats/anomaly?" + urlencode(QUERY), {"day": "2026-11-03"})
    assert status == 200
    with Session(engine) as session:
        assert session.get(StatsAnomalyDay, date(2026, 11, 3)) is not None
    client.post("/stats/anomaly?" + urlencode(QUERY), {"day": "2026-11-03"})
    with Session(engine) as session:
        assert session.get(StatsAnomalyDay, date(2026, 11, 3)) is None
