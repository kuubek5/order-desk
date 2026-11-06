"""Вкладки «Пошта», «Техніки», «Виробництво»: дані, крайні випадки, справжні роути.

Правило «виконано» тут не перевіряється вдруге, воно в `test_stats_done_rule.py`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import stats as st
from app.db import Base
from app.models import EmailMessage, MachineReading, Order, ReworkRecord, StatusEvent
from app.services import stats_data as data
from app.services import stats_export, stats_mail, stats_production, stats_techs, stats_view
from app.statuses import STATUS_FOUND, STATUS_ISSUED
from tests.test_stats import ADMIN, OPERATOR, _client, app_db  # noqa: F401 — фікстура

TUE, WED = date(2026, 11, 3), date(2026, 11, 4)
QUERY = {"period": "custom", "from": "2026-10-05", "to": "2026-11-06"}


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


def _params(tab="overview", **extra) -> stats_view.Params:
    return stats_view.parse_params({**QUERY, "tab": tab, **extra}, date(2026, 11, 6), True)


def _letter(db, at, name="Клієнт", category=None, guess="mono a3") -> EmailMessage:
    letter = EmailMessage(
        uid=f"u{at.isoformat()}{name}", uid_validity="1", from_address="a@b.c", from_name=name, subject="s",
        status="нове", attachments_status="ready", received_at=at, filter_category=category,
        material_color_guess=guess,
    )
    db.add(letter)
    db.flush()
    return letter


def _work(db, tab="03.11.26", source="lab", qty="2", sum3d="10-00-00", **kw) -> Order:
    order = Order(source=source, sheet_tab=tab, quantity=qty, sum3d_id=sum3d, material_color="моно а3", **kw)
    db.add(order)
    db.flush()
    return order


# ── Пошта ────────────────────────────────────────────────────────────────────
def test_letter_map_counts_letters_and_skips_filtered(db):
    _letter(db, datetime(2026, 11, 3, 12, 15))
    _letter(db, datetime(2026, 11, 3, 12, 45))
    _letter(db, datetime(2026, 11, 3, 12, 50), category="3D-друк")
    db.commit()
    heat = stats_mail.letter_heat(db, [TUE])
    assert heat["n"] == 2
    assert heat["grid"][1][12] == 2, "вівторок = рядок 1, 12:00"


def test_letters_before_the_rollover_belong_to_the_previous_day(db):
    _letter(db, datetime(2026, 11, 4, 7, 0))
    _letter(db, datetime(2026, 11, 4, 7, 45))
    db.commit()
    assert stats_mail.letter_heat(db, [TUE])["n"] == 1
    assert stats_mail.letter_heat(db, [WED])["n"] == 1


def test_friday_carries_weekend_letters(db):
    _letter(db, datetime(2026, 11, 7, 12, 0))
    db.commit()
    assert stats_mail.letter_heat(db, [date(2026, 11, 6)])["n"] == 1


def test_cell_lists_exactly_the_letters_of_the_map(db):
    _letter(db, datetime(2026, 11, 3, 12, 15), name="Перший")
    _letter(db, datetime(2026, 11, 3, 13, 15), name="Другий")
    db.commit()
    assert [row["who"] for row in stats_mail.letters_in_cell(db, [TUE], 1, 12)] == ["Перший"]


def test_client_filter_applies_to_the_map(db):
    _letter(db, datetime(2026, 11, 3, 12, 15), name="Перший")
    _letter(db, datetime(2026, 11, 3, 12, 15), name="Другий")
    db.commit()
    assert stats_mail.letter_heat(db, [TUE], st.client_key("Перший"))["n"] == 1


def _rhythm(*gaps, since):
    today = date(2026, 11, 6)
    day = today - timedelta(days=since)
    days = {day}
    for gap in gaps:
        day -= timedelta(days=gap)
        days.add(day)
    return stats_mail.rhythm_of(days, today)


@pytest.mark.parametrize(
    "now, was, rhythm, expected",
    [
        (0, 40, _rhythm(2, 2, 2, 2, since=20), "gone"),
        (0, 5, _rhythm(30, 30, 30, since=25), "quiet"),  # раз на місяць: ще не зник
        (0, 5, _rhythm(30, 30, 30, since=95), "gone"),
        (0, 5, _rhythm(2, since=20), "quiet"),  # ритму ще немає
        (5, 0, _rhythm(2, since=1), "new"),
        (20, 0, _rhythm(7, 7, 7, 7, 7, since=1), "up"),  # повернувся, а не новий
        (20, 10, _rhythm(2, 2, 2, 2, since=1), "up"),
        (10, 20, _rhythm(2, 2, 2, 2, since=1), "down"),
        (21, 20, _rhythm(2, 2, 2, 2, since=1), "steady"),
        (2, 4, _rhythm(2, 2, 2, 2, since=1), "steady"),  # одиниці не роблять тренду
    ],
)
def test_trend_follows_the_clients_own_rhythm(now, was, rhythm, expected):
    assert stats_mail.trend_of(now, was, rhythm) == expected


def test_mail_tab_ignores_source_filter(db):
    _work(db, source="email", client_name="Клієнт", qty="5")
    _work(db, source="lab", qty="9", technician_name="с")
    db.commit()
    built = stats_mail.build(db, _params("mail", src="lab"))
    assert built["units_n"] == 5
    assert [row["label"] for row in built["clients"]] == ["Клієнт"]


def test_gone_client_is_listed_though_it_has_no_units_now(db):
    # Sum3D різний: той самий час на різних вкладках правило вважає копією рядка.
    for index, day in enumerate(("01.09.26", "03.09.26", "07.09.26", "09.09.26", "10.09.26")):
        _work(db, tab=day, source="email", client_name="Зник", qty="3", sum3d=f"1{index}-00-00")
    db.commit()
    built = stats_mail.build(db, _params("mail"))
    assert [row["label"] for row in built["gone"]] == ["Зник"]
    assert built["clients"] == []


# ── Техніки ──────────────────────────────────────────────────────────────────
def test_tech_row_sums_done_work_under_one_key(db):
    _work(db, technician_name="с", qty="3", cam_comment="на швидку")
    _work(db, technician_name="c", qty="2")  # латинська «c» — той самий технік
    _work(db, technician_name="с", qty="9", sum3d=None)
    db.commit()
    rows = stats_techs.build(db, _params("techs"))["rows"]
    assert len(rows) == 1
    assert rows[0]["units"] == 5 and rows[0]["works"] == 2
    assert rows[0]["rush_pct"] == 50.0


def test_tech_speed_is_honest_about_how_many_measures(db):
    seen = datetime(2026, 11, 3, 9, 0)
    _work(db, technician_name="с", row_seen_at=seen, job_code_at=seen + timedelta(minutes=30))
    _work(db, technician_name="с", row_seen_at=seen, job_code_at=seen + timedelta(minutes=90))
    _work(db, technician_name="с", row_seen_at=seen, job_code_at=seen - timedelta(minutes=5))
    db.commit()
    row = stats_techs.build(db, _params("techs"))["rows"][0]
    assert row["median"] == pytest.approx(60.0) and row["p80"] == pytest.approx(90.0)
    assert row["n"] == 2 and row["few"] is True


def test_tech_rework_counts_only_their_own_fault(db):
    order = _work(db, technician_name="с", qty="10")
    db.add(ReworkRecord(order_id=order.id, blame="технік", redo_quantity="2"))
    db.add(ReworkRecord(order_id=order.id, blame="обладнання", redo_quantity="5"))
    db.commit()
    row = stats_techs.build(db, _params("techs"))["rows"][0]
    assert row["rework_units"] == 2 and row["rework_pct"] == 20.0


def test_tech_sort_keeps_empty_speed_at_the_bottom(db):
    seen = datetime(2026, 11, 3, 9, 0)
    _work(db, technician_name="Швидкий", row_seen_at=seen, job_code_at=seen + timedelta(minutes=10))
    _work(db, technician_name="Повільний", row_seen_at=seen, job_code_at=seen + timedelta(hours=9))
    _work(db, technician_name="Безвимірів")
    db.commit()
    for order, expected in (("median", "Швидкий"), ("-median", "Повільний")):
        rows = stats_techs.build(db, _params("techs", ts=order))["rows"]
        assert rows[0]["label"] == expected
        assert rows[-1]["label"] == "Безвимірів"


def test_unknown_sort_is_ignored(db):
    assert _params("techs", ts="id; drop").ts == ""


def test_percentile_is_nearest_rank():
    assert st.percentile([], 80) is None
    assert st.percentile([5.0], 80) == 5.0
    assert st.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 80) == 4.0
    assert st.percentile([1.0, 2.0, 3.0, 4.0], 80) == 4.0


# ── Виробництво ──────────────────────────────────────────────────────────────
def _reading(db, at, percent, sum3d="10-00-00", program=None):
    db.add(MachineReading(host="m", captured_at=at, percent=percent, sum3d_id=sum3d, program_at=program))


def test_mill_stage_runs_from_first_progress_to_first_hundred(db):
    """Диск годину чекає на 0 % і пів ночі стоїть на 100 %: це не фрезерування."""
    program = datetime(2026, 11, 3, 10, 0)
    _work(db, sum3d="10-00-00")
    _reading(db, datetime(2026, 11, 3, 10, 5), 0, program=program)
    _reading(db, datetime(2026, 11, 3, 11, 0), 5, program=program)
    _reading(db, datetime(2026, 11, 3, 13, 0), 100, program=program)
    _reading(db, datetime(2026, 11, 3, 23, 0), 100, program=program)
    db.commit()
    stages = {s["label"]: s for s in stats_production.build(db, _params("production"))["lab"]["stages"]}
    assert stages["Sum3D → старт фрезерування"]["median"] == 60.0
    assert stages["Фрезерування"]["median"] == 120.0


def test_program_through_midnight_stays_one_run(db):
    program = datetime(2026, 11, 3, 22, 0)
    _work(db, sum3d="22-00-00")
    _reading(db, datetime(2026, 11, 3, 22, 30), 5, sum3d="22-00-00", program=program)
    _reading(db, datetime(2026, 11, 4, 2, 30), 100, sum3d="22-00-00", program=program)
    db.commit()
    stages = {s["label"]: s for s in stats_production.build(db, _params("production"))["lab"]["stages"]}
    assert stages["Фрезерування"]["median"] == 240.0


def test_same_clock_on_another_day_is_someone_elses_disc(db):
    """Sum3D ID — лише час доби: запис верстата за тиждень — це інший диск."""
    _work(db, sum3d="10-00-00")
    program = datetime(2026, 11, 10, 10, 0)
    _reading(db, datetime(2026, 11, 10, 10, 30), 5, program=program)
    _reading(db, datetime(2026, 11, 10, 12, 30), 100, program=program)
    db.commit()
    stages = {s["label"]: s for s in stats_production.build(db, _params("production"))["lab"]["stages"]}
    assert stages["Фрезерування"]["n"] == 0


def test_several_discs_use_the_first_program(db):
    program = datetime(2026, 11, 3, 12, 41)
    _work(db, sum3d="12-37-51\n12-41-48")
    _reading(db, datetime(2026, 11, 3, 13, 0), 5, sum3d="12-41-48", program=program)
    _reading(db, datetime(2026, 11, 3, 14, 0), 100, sum3d="12-41-48", program=program)
    db.commit()
    stages = {s["label"]: s for s in stats_production.build(db, _params("production"))["lab"]["stages"]}
    assert stages["Фрезерування"]["n"] == 1


def test_client_stages_use_the_letter_time_and_status_events(db):
    letter = _letter(db, datetime(2026, 11, 3, 8, 0))
    order = _work(db, source="email", client_name="Клієнт", sum3d="10-00-00", source_email_id=letter.id)
    # Статуси в базі в UTC: Київ = +2 у листопаді.
    db.add(StatusEvent(order_id=order.id, status=STATUS_FOUND, occurred_at=datetime(2026, 11, 4, 8, 0)))
    db.add(StatusEvent(order_id=order.id, status=STATUS_ISSUED, occurred_at=datetime(2026, 11, 4, 9, 0)))
    db.commit()
    stages = {s["label"]: s for s in stats_production.build(db, _params("production"))["client"]["stages"]}
    assert stages["Лист → Sum3D"]["median"] == 120.0
    assert stages["Знайдено → видано"]["median"] == 60.0


def test_negative_interval_is_dropped(db):
    seen = datetime(2026, 11, 3, 12, 0)
    _work(db, sum3d="10-00-00", row_seen_at=seen, job_code_at=seen - timedelta(hours=1))
    db.commit()
    stages = {s["label"]: s for s in stats_production.build(db, _params("production"))["lab"]["stages"]}
    assert stages["Наряд → модель"]["n"] == 0
    assert stages["Наряд → модель"]["median_text"] == "—"


def test_longest_stage_needs_enough_measures(db):
    seen = datetime(2026, 11, 3, 9, 0)
    for index in range(10):
        _work(db, sum3d="10-00-00", row_seen_at=seen, job_code_at=seen + timedelta(minutes=30), work_order_no=f"{index}")
    # Один довгий вимір іншого етапу не мусить стати «найдовшим».
    _work(db, sum3d="10-00-00", row_seen_at=seen, work_order_no="x", job_code_at=seen + timedelta(minutes=31))
    db.commit()
    built = stats_production.build(db, _params("production"))["lab"]
    assert built["longest"] == "Наряд → модель"


def test_buckets_add_up(db):
    rows = [
        {"a": datetime(2026, 1, 1, 0, 0), "b": datetime(2026, 1, 1, 0, 0) + timedelta(minutes=m)}
        for m in (30, 120, 600, 3000)
    ]
    stage = stats_production.stage_stats(rows, "a", "b", "x")
    assert [b["n"] for b in stage["buckets"]] == [1, 1, 1, 1]
    assert sum(b["pct"] for b in stage["buckets"]) == 100.0


# ── Справжні роути ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "tab, marker",
    [("mail", "Коли приходять листи"), ("techs", "Наряд → модель, медіана"), ("production", "Модель → Sum3D")],
)
def test_tabs_render_for_admin_even_when_empty(app_db, tab, marker):  # noqa: F811
    client = _client(ADMIN)
    status, _, body = client.get("/stats?" + urlencode({**QUERY, "tab": tab}))
    assert status == 200 and marker in body
    assert "· етап" not in body.split("Прогноз")[0], "вкладка більше не заглушка"
    status, headers, payload = client.get("/stats/export?" + urlencode({**QUERY, "tab": tab}))
    assert status == 200


def test_operator_still_cannot_open_techs(app_db):  # noqa: F811
    _, _, body = _client(OPERATOR).get("/stats?" + urlencode({**QUERY, "tab": "techs"}))
    assert "Наряд → модель, медіана</a>" not in body


def test_mail_cell_route_lists_letters(app_db, engine):  # noqa: F811
    with Session(engine) as session:
        _letter(session, datetime(2026, 11, 3, 12, 15), name="Перший")
        session.commit()
    client = _client(ADMIN)
    status, _, body = client.get("/stats/list?" + urlencode({**QUERY, "cell": "mail:1:12"}))
    assert status == 200 and "Перший" in body and "1</b> листів" in body


def test_workbooks_build_with_data(db):
    _letter(db, datetime(2026, 11, 3, 12, 15))
    _work(db, technician_name="с", source="lab")
    _work(db, source="email", client_name="Клієнт")
    db.commit()
    for book, tab in (
        (stats_export.mail_book, "mail"),
        (stats_export.techs_book, "techs"),
        (stats_export.production_book, "production"),
    ):
        assert book(db, _params(tab))[:2] == b"PK"
