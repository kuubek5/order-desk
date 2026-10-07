"""Щоденний знімок бази і перевірка знімків після запису (DR-навчання 07.10.26)."""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine

from app import daily_backup, monthly_backup, pre_update_backup
from app.daily_backup import KEEP, REFRESH_AFTER_SECONDS, ensure_daily_snapshot, list_daily
from app.snapshot_tools import SnapshotError, inspect_snapshot


@pytest.fixture
def live(tmp_path):
    db = tmp_path / "kuubmill.db"
    con = sqlite3.connect(db)
    con.execute("create table orders(id integer primary key, note text)")
    con.executemany("insert into orders(note) values (?)", [("x",)] * 25)
    con.commit()
    con.close()
    engine = create_engine(f"sqlite:///{db}")
    yield engine, db
    engine.dispose()


def test_first_tick_of_the_day_creates_a_verified_snapshot(live):
    engine, db = live
    path = ensure_daily_snapshot(engine, db, today=date(2026, 10, 7), now=1_000_000.0)
    assert path.name == "kuubmill-daily-2026-10-07.db"
    info = inspect_snapshot(path)
    assert info.ok and info.orders == 25


def test_a_fresh_snapshot_is_not_rewritten_but_a_stale_one_is_refreshed(live):
    engine, db = live
    first = ensure_daily_snapshot(engine, db, today=date(2026, 10, 7), now=os.path.getmtime(db) + 1)
    mtime = first.stat().st_mtime
    assert ensure_daily_snapshot(engine, db, today=date(2026, 10, 7), now=mtime + 60) is None

    con = sqlite3.connect(db)
    con.execute("insert into orders(note) values ('later')")
    con.commit()
    con.close()
    again = ensure_daily_snapshot(engine, db, today=date(2026, 10, 7), now=mtime + REFRESH_AFTER_SECONDS + 5)
    assert again == first and inspect_snapshot(again).orders == 26, "день тримає найсвіжіший стан"


def test_only_the_last_days_are_kept(live):
    engine, db = live
    for d in range(1, KEEP + 6):
        ensure_daily_snapshot(engine, db, today=date(2026, 9, d), now=1_000_000.0 + d)
    names = [p.name for p in list_daily(db)]
    assert len(names) == KEEP
    assert names[0] == f"kuubmill-daily-2026-09-{KEEP + 5:02d}.db" and "kuubmill-daily-2026-09-01.db" not in names


def test_a_snapshot_that_fails_verification_is_not_left_behind(live, monkeypatch):
    engine, db = live

    def corrupt(path):
        raise SnapshotError("зіпсовано")

    monkeypatch.setattr(daily_backup, "verify_integrity", corrupt)
    with pytest.raises(Exception, match="зіпсовано"):
        ensure_daily_snapshot(engine, db, today=date(2026, 10, 7))
    assert list_daily(db) == []
    assert not list((db.parent / "backups" / "daily").glob("*.tmp"))


def test_monthly_snapshot_is_verified_before_it_takes_its_name(live, monkeypatch):
    engine, db = live
    monkeypatch.setattr(monthly_backup, "verify_integrity", lambda p: (_ for _ in ()).throw(RuntimeError("зіпсовано")))
    with pytest.raises(RuntimeError):
        monthly_backup.ensure_monthly_snapshot(engine, db, today=date(2026, 10, 7))
    assert monthly_backup.list_snapshots(db) == []
    assert not list(monthly_backup.backups_dir(db).glob("*.tmp"))


def test_pre_update_snapshot_is_verified_before_it_takes_its_name(live, monkeypatch):
    engine, db = live
    monkeypatch.setattr(pre_update_backup, "verify_integrity", lambda p: (_ for _ in ()).throw(RuntimeError("зіпсовано")))
    with pytest.raises(RuntimeError):
        pre_update_backup.snapshot_before_update(engine, db, "0.21.52", now=datetime(2026, 10, 7, 12, 0, 0))
    assert pre_update_backup.list_pre_update_snapshots(db) == []


def test_good_monthly_and_pre_update_snapshots_still_work(live):
    engine, db = live
    monthly = monthly_backup.ensure_monthly_snapshot(engine, db, today=date(2026, 10, 7))
    pre = pre_update_backup.snapshot_before_update(engine, db, "0.21.52", now=datetime(2026, 10, 7, 12, 0, 0))
    assert inspect_snapshot(monthly).ok and inspect_snapshot(pre).ok
