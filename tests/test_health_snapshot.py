"""Звірка «після оновлення нічого не зникло».

Оновлення тягне міграції схеми. Досі єдиним доказом «усе на місці» було
відчуття оператора: база могла втратити рядки, і побачив би це той, кому
конкретна робота знадобилась би через тиждень. Тепер застосунок сам рахує
рядки перед оновленням і звіряє після першого старту нової версії.

Стережемо чотири речі:
1. втрата помічається і називає таблицю;
2. РІСТ втратою не вважається (синк додав роботи — це норма);
3. таблиці, які застосунок сам чистить за розкладом, у звіт не потрапляють;
4. звірка не спрацьовує, поки версія не змінилась (оновлення могло не доїхати).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.__version__ import VERSION
from app.db import Base
from app.models import Order, User
from app.services.health_snapshot import (
    BEFORE_KEY,
    REPORT_KEY,
    capture,
    check_after_update,
    compare,
    last_report,
    remember_before_update,
)
from app.settings_store import get_setting, set_setting


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(username="op", password_hash="x", full_name="op", role="оператор"))
        for i in range(3):
            session.add(Order(source="lab", sheet_tab="01.09.26", row_number=i + 1, status="нове"))
        session.commit()
        yield session


def test_capture_counts_every_table(db):
    snapshot = capture(db)
    assert snapshot["version"] == VERSION
    assert snapshot["tables"]["orders"] == 3
    assert snapshot["tables"]["users"] == 1
    # Не «кілька таблиць», а всі: саме забуті таблиці й були проблемою бекапу.
    assert set(snapshot["tables"]) == set(Base.metadata.tables)


def test_lost_rows_are_reported_with_the_table_name():
    before = {"version": "0.11.2", "tables": {"orders": 3436, "clients": 249}}
    after = {"version": "0.11.3", "at": "2026-09-07T09:00:00", "tables": {"orders": 3424, "clients": 249}}
    report = compare(before, after)
    assert report["ok"] is False
    assert report["lost"] == {"orders": [3436, 3424]}
    assert report["from_version"] == "0.11.2"
    assert report["to_version"] == "0.11.3"


def test_growth_is_not_a_loss():
    before = {"version": "0.11.2", "tables": {"orders": 100}}
    after = {"version": "0.11.3", "tables": {"orders": 137}}
    assert compare(before, after)["ok"] is True


def test_self_pruning_tables_are_allowed_to_shrink():
    """Історія показань печей живе 30 днів — її зменшення робота прибиральника."""
    before = {"version": "0.11.2", "tables": {"furnace_readings": 40000, "orders": 10}}
    after = {"version": "0.11.3", "tables": {"furnace_readings": 12000, "orders": 10}}
    assert compare(before, after)["ok"] is True


def test_check_does_nothing_while_the_version_is_unchanged(db):
    remember_before_update(db)
    db.commit()
    assert check_after_update(db) is None
    # Знімок лишається: оновлення могло не встановитись, звірка ще попереду.
    assert get_setting(db, BEFORE_KEY)


def test_check_after_a_version_change_reports_and_clears_the_snapshot(db):
    stale = capture(db)
    stale["version"] = "0.0.1-старе"
    stale["tables"]["orders"] = 10  # ніби до оновлення робіт було більше
    set_setting(db, BEFORE_KEY, json.dumps(stale))
    db.commit()

    report = check_after_update(db)
    db.commit()

    assert report is not None
    assert report["ok"] is False
    assert report["lost"]["orders"] == [10, 3]
    assert report["to_version"] == VERSION
    assert not get_setting(db, BEFORE_KEY), "знімок «до» мав прибратись після звірки"
    assert last_report(db)["lost"]["orders"] == [10, 3]
    assert json.loads(get_setting(db, REPORT_KEY))["ok"] is False


def test_a_clean_update_reports_ok_with_totals(db):
    stale = capture(db)
    stale["version"] = "0.0.1-старе"
    set_setting(db, BEFORE_KEY, json.dumps(stale))
    db.commit()

    report = check_after_update(db)
    assert report["ok"] is True
    assert report["totals"]["orders"] == 3


def test_broken_snapshot_does_not_crash_the_start(db):
    set_setting(db, BEFORE_KEY, "{не json")
    db.commit()
    assert check_after_update(db) is None


@pytest.mark.parametrize(
    "n, expected",
    [(1, "1 верстат"), (2, "2 верстати"), (5, "5 верстатів"), (11, "11 верстатів"), (21, "21 верстат")],
)
def test_totals_are_declined_properly(n, expected):
    """«1 верстатів» на екрані читається як недбалість — оператор має вірити
    числам, які йому показують після оновлення."""
    report = compare(
        {"version": "0.1", "tables": {"machines": n}},
        {"version": "0.2", "tables": {"machines": n}},
    )
    assert report["totals_text"] == expected
