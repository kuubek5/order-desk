"""Правило «виконано» в статистиці (власник 06.11.26).

Бойовий випадок: екран показав 116 443 одиниці за 30 днів при реальних 6 720. Три
рядки зі зсунутими колонками дали 108 810, ще 887 дали рядки без Sum3D: перенесена
робота лічилась на кожній вкладці, куди її переносили. Правило одне на всі цифри, і
цей файл перевіряє саме це: одиниці, клієнти, кольори, карта, бічний список.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Material, Order, ReworkRecord, VyrobitokCell
from app.services import stats_data as data

TUE, WED = date(2026, 11, 3), date(2026, 11, 4)
DAYS = [TUE, WED]


@pytest.fixture
def db():
    data.clear_cache()
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    data.clear_cache()


def _facts(db) -> dict:
    return data.summarize(data.facts(db, DAYS, data.Filters()), DAYS, data._material_group_map(db))


def _lab(db, tab="03.11.26", qty="2", sum3d="10-00-00", **kw) -> Order:
    order = Order(source="lab", sheet_tab=tab, quantity=qty, sum3d_id=sum3d, material_color="моно а3", **kw)
    db.add(order)
    db.flush()
    return order


@pytest.mark.parametrize(
    "sum3d, counted",
    [
        ("12-01-45", True),
        ("8-05-11", True),
        ("20-57-15\n21-36-49", True),  # кілька дисків у клітинці
        ("_04-03-20", True),  # позначка диска
        ("2026-11-02_13-12-29", True),  # повна назва
        (" 12-01-45 ", True),
        (None, False),
        ("", False),
        ("робота за 30.10", False),  # рядок зі зсунутими колонками
        ("немає папки", False),  # текст оператора
        ("дайте преміли", False),
    ],
)
def test_sum3d_must_hold_a_time(db, sum3d, counted):
    _lab(db, sum3d=sum3d)
    db.commit()
    assert _facts(db)["units"] == (2 if counted else 0)
    assert data.sum3d_has_time(sum3d) is counted, "двійник у Python розійшовся з SQL"


@pytest.mark.parametrize(
    "mill_count, counted",
    [(None, True), ("", True), ("1", True), ("2", False), ("3", False), (" 2 ", False), ("31,66666667", True)],
)
def test_rework_is_not_counted_again(db, mill_count, counted):
    _lab(db, mill_count=mill_count)
    db.commit()
    assert _facts(db)["units"] == (2 if counted else 0)


def test_shifted_row_quantity_is_not_summed(db):
    """id 7533: кількість 37875 — це номер наряду, а Sum3D міг би бути й часом."""
    _lab(db, qty="200")
    _lab(db, qty="201")
    _lab(db, qty="37875", sum3d="робота за 30.10", archived_at=datetime(2026, 11, 3, 10, 10))
    db.commit()
    result = _facts(db)
    assert result["units"] == 200 and result["works"] == 1


def test_archived_done_work_still_counts(db):
    """Лаба чистить старі вкладки: серед виконаних архівних 22 %, викидати їх не можна."""
    _lab(db, archived_at=datetime(2026, 11, 20, 8, 0))
    db.commit()
    assert _facts(db)["units"] == 2


def test_moved_work_counts_once_on_the_day_it_was_done(db):
    _lab(db, tab="03.11.26", sum3d=None, work_order_no="38124")
    _lab(db, tab="04.11.26", sum3d="10-00-00", work_order_no="38124")
    db.commit()
    result = _facts(db)
    assert result["units"] == 2
    assert result["by_day"][TUE]["works"] == 0
    assert result["by_day"][WED]["works"] == 1


def test_done_work_without_quantity_is_still_flagged(db):
    _lab(db, qty=None)
    _lab(db, qty="2+1")
    _lab(db, qty=None, sum3d=None)
    db.commit()
    assert _facts(db)["noqty"] == 2


def test_every_figure_uses_the_same_rule(db):
    good = Order(source="email", sheet_tab="03.11.26", quantity="5", sum3d_id="12-00-00",
                 client_name="Клієнт", material_color="mono a3", row_number=50)
    undone = Order(source="email", sheet_tab="03.11.26", quantity="7", client_name="Клієнт",
                   material_color="mono a3", row_number=51)
    ghost = Order(source="sheet_client", sheet_tab="03.11.26", quantity="36026", sum3d_id="10-00-00",
                  client_name="Клієнт", material_color="2", row_number=52)
    db.add_all([good, undone, ghost])
    db.commit()

    assert data.client_units(db, DAYS, data.Filters())["клієнт"]["units"] == 5
    assert [c["color"] for c in data.color_units(db, DAYS, data.Filters())] == ["mono a3"]
    assert data.sum3d_heat(db, DAYS, data.Filters())["n"]["client"] == 1
    assert [w["id"] for w in data.works_list(db, [TUE], data.Filters())] == [good.id]


def test_row_map_shows_what_arrived_but_not_shifted_rows(db):
    """Карта появи рядка — про надходження: без Sum3D рахується, зсунутий рядок ні."""
    seen = datetime(2026, 11, 3, 9, 0)
    _lab(db, sum3d=None, row_seen_at=seen)
    _lab(db, qty="37875", sum3d=None, row_seen_at=seen)
    db.commit()
    heat = data.row_seen_heat(db, DAYS, data.Filters())
    assert heat["n"]["lab"] == 1


def test_technician_filter_skips_shifted_rows(db):
    _lab(db, technician_name="кіра")
    _lab(db, qty="37875", technician_name="2026-10-31_00001-004")
    db.commit()
    labels = {group["label"] for group in data.tech_groups(db).values()}
    assert labels == {"кіра"}


def test_rework_list_is_not_filtered_by_the_rule(db):
    """Брак — це саме переробки: у них часто порожня кількість і «який раз» = 2."""
    db.add(Material(name="Цирконій", is_production=True, sort_order=1))
    order = _lab(db, qty=None, mill_count="2")
    db.add(ReworkRecord(order_id=order.id, blame="технік", redo_quantity="1"))
    db.commit()
    assert data.rework_summary(db, DAYS, data.Filters())["units"] == 1
    assert [w["id"] for w in data.works_list(db, DAYS, data.Filters(), rework=True)] == [order.id]


# ── SLM окремо: числа з клітинок «Виробітку» ──────────────────────────────────
def _slm(db, day=TUE, lab=None, mail=None, lab_override=None) -> None:
    if lab is not None or lab_override is not None:
        db.add(VyrobitokCell(day=day, col_key="lab_slm", auto_value=lab, override_value=lab_override))
    if mail is not None:
        db.add(VyrobitokCell(day=day, col_key="mail_slm", auto_value=mail))
    db.flush()


def test_slm_stands_apart_from_every_total(db):
    _lab(db)
    _slm(db, lab=10, mail=5)
    db.commit()
    result = _facts(db)
    assert result["units"] == 2 and result["lab"] == 2 and result["client"] == 0
    assert sum(result["materials"].values()) == 2
    assert data.slm_summary(db, DAYS, data.Filters()) == {"lab": 10, "client": 5, "units": 15}


def test_slm_operator_correction_wins_as_in_vyrobitok(db):
    _slm(db, lab=10, lab_override=3)
    db.commit()
    assert data.slm_summary(db, DAYS, data.Filters())["units"] == 3


def test_slm_counts_only_days_of_the_period(db):
    _slm(db, day=date(2026, 11, 2), lab=100)
    _slm(db, day=TUE, lab=10)
    db.commit()
    assert data.slm_summary(db, DAYS, data.Filters())["units"] == 10


def test_slm_follows_the_source_filter(db):
    _slm(db, lab=10, mail=5)
    db.commit()
    assert data.slm_summary(db, DAYS, data.Filters(src="lab"))["units"] == 10
    assert data.slm_summary(db, DAYS, data.Filters(src="client"))["units"] == 5


@pytest.mark.parametrize("filters", [{"mat": "1"}, {"mat": "none"}, {"tech": "кіра"}, {"client": "клієнт"}])
def test_slm_has_no_answer_for_filters_about_works(db, filters):
    _slm(db, lab=10)
    db.commit()
    assert data.slm_summary(db, DAYS, data.Filters(**filters)) is None

