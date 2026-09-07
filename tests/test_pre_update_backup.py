"""Копія бази перед оновленням (B.6): знімається, читається, не розростається."""

import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from app.pre_update_backup import (
    KEEP,
    list_pre_update_snapshots,
    pre_update_dir,
    snapshot_before_update,
)


def _seeded_db(tmp_path: Path) -> tuple[object, Path]:
    db_path = tmp_path / "kuubmill.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE work (id INTEGER PRIMARY KEY, naryad TEXT)"))
        conn.execute(text("INSERT INTO work (naryad) VALUES ('24122'), ('24123')"))
    return engine, db_path


def test_snapshot_is_a_readable_copy_of_the_live_db(tmp_path):
    engine, db_path = _seeded_db(tmp_path)

    created = snapshot_before_update(engine, db_path, "0.11.4")

    assert created.exists()
    assert created.parent == pre_update_dir(db_path)
    with sqlite3.connect(created) as conn:
        rows = conn.execute("SELECT naryad FROM work ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["24122", "24123"]


def test_filename_carries_version_and_time(tmp_path):
    engine, db_path = _seeded_db(tmp_path)

    created = snapshot_before_update(
        engine, db_path, "0.11.4", now=datetime(2026, 9, 7, 8, 30, 15)
    )

    assert created.name == "kuubmill-pre-0.11.4-20260907-083015.db"


def test_version_cannot_escape_the_backup_folder(tmp_path):
    engine, db_path = _seeded_db(tmp_path)

    created = snapshot_before_update(engine, db_path, "../../evil 0.1")

    assert created.parent == pre_update_dir(db_path)
    assert ".." not in created.name


def test_only_the_last_keep_snapshots_survive(tmp_path):
    engine, db_path = _seeded_db(tmp_path)

    for minute in range(KEEP + 3):
        snapshot_before_update(
            engine, db_path, "0.11.4", now=datetime(2026, 9, 7, 8, minute, 0)
        )

    kept = list_pre_update_snapshots(db_path)
    assert len(kept) == KEEP
    # Найновіша перша, найстаріші прибрані.
    assert kept[0].name.endswith("-20260907-080700.db")
    assert all("080000" not in p.name for p in kept)


def test_leftover_tmp_from_a_crashed_attempt_does_not_block(tmp_path):
    engine, db_path = _seeded_db(tmp_path)
    folder = pre_update_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "kuubmill-pre-0.11.4-20260907-083015.db.tmp").write_bytes(b"junk")

    created = snapshot_before_update(
        engine, db_path, "0.11.4", now=datetime(2026, 9, 7, 8, 30, 15)
    )

    assert created.exists()
    assert not (folder / (created.name + ".tmp")).exists()


def test_missing_folder_lists_as_empty(tmp_path):
    assert list_pre_update_snapshots(tmp_path / "kuubmill.db") == []
