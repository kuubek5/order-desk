from datetime import date
from pathlib import Path

import sqlite3

from sqlalchemy import create_engine, text

from app.monthly_backup import (
    backups_dir,
    ensure_monthly_snapshot,
    list_snapshots,
    month_label_uk,
    previous_month,
    snapshot_filename,
)


def _seeded_db(tmp_path: Path) -> tuple[object, Path]:
    db_path = tmp_path / "orderdesk.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE work (id INTEGER PRIMARY KEY, naryad TEXT)"))
        conn.execute(text("INSERT INTO work (naryad) VALUES ('24122'), ('24123')"))
    return engine, db_path


def test_previous_month_wraps_january():
    assert previous_month(date(2026, 1, 15)) == (2025, 12)
    assert previous_month(date(2026, 8, 3)) == (2026, 7)


def test_snapshot_filename_is_zero_padded_iso():
    assert snapshot_filename(2026, 7) == "kuubmill-2026-07.db"
    assert snapshot_filename(2026, 12) == "kuubmill-2026-12.db"


def test_month_label_uk():
    assert month_label_uk(2026, 7) == "липень 2026"


def test_ensure_creates_previous_month_snapshot(tmp_path):
    engine, db_path = _seeded_db(tmp_path)
    created = ensure_monthly_snapshot(engine, db_path, today=date(2026, 8, 2))

    assert created is not None
    assert created.name == "kuubmill-2026-07.db"
    assert created.parent == backups_dir(db_path)
    # The snapshot is a real, queryable SQLite copy with the data intact.
    con = sqlite3.connect(created)
    try:
        rows = con.execute("SELECT naryad FROM work ORDER BY naryad").fetchall()
    finally:
        con.close()
    assert [r[0] for r in rows] == ["24122", "24123"]


def test_ensure_is_idempotent_within_the_month(tmp_path):
    engine, db_path = _seeded_db(tmp_path)
    first = ensure_monthly_snapshot(engine, db_path, today=date(2026, 8, 2))
    assert first is not None
    # A later tick in the same month must NOT rewrite it.
    second = ensure_monthly_snapshot(engine, db_path, today=date(2026, 8, 20))
    assert second is None
    assert len(list_snapshots(db_path)) == 1


def test_new_month_produces_a_second_snapshot(tmp_path):
    engine, db_path = _seeded_db(tmp_path)
    ensure_monthly_snapshot(engine, db_path, today=date(2026, 8, 2))  # July
    ensure_monthly_snapshot(engine, db_path, today=date(2026, 9, 1))  # August

    names = [p.name for p in list_snapshots(db_path)]
    assert names == ["kuubmill-2026-08.db", "kuubmill-2026-07.db"]  # newest first


def test_leftover_tmp_file_does_not_block_snapshot(tmp_path):
    engine, db_path = _seeded_db(tmp_path)
    folder = backups_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    # Simulate a crash mid-copy that left a stale .tmp behind.
    (folder / "kuubmill-2026-07.db.tmp").write_bytes(b"garbage")

    created = ensure_monthly_snapshot(engine, db_path, today=date(2026, 8, 2))
    assert created is not None and created.exists()
    assert not (folder / "kuubmill-2026-07.db.tmp").exists()


def test_legacy_orderdesk_prefix_still_lists_alongside_new_prefix(tmp_path):
    """F18: KuubMill renamed monthly snapshots from `orderdesk-` to
    `kuubmill-` (CLAUDE.md §14 rename migrations). Old files on a machine
    that's been running since before the rename must stay visible — the
    admin's backup screen listing them is the whole point of a snapshot."""
    engine, db_path = _seeded_db(tmp_path)
    folder = backups_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "orderdesk-2026-07.db").write_bytes(b"legacy snapshot")
    (folder / "kuubmill-2026-08.db").write_bytes(b"new snapshot")

    names = [p.name for p in list_snapshots(db_path)]
    assert set(names) == {"orderdesk-2026-07.db", "kuubmill-2026-08.db"}
    # Newest first despite "k" < "o" sorting the other way lexically.
    assert names == ["kuubmill-2026-08.db", "orderdesk-2026-07.db"]

    # A fresh snapshot after the rename is always written under the new name.
    created = ensure_monthly_snapshot(engine, db_path, today=date(2026, 10, 1))
    assert created is not None
    assert created.name == "kuubmill-2026-09.db"
