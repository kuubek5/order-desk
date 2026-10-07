"""Безпечне відновлення зі знімка (DR-навчання 07.10.26).

Кожен тест — реальна аварія, відтворена на справжніх файлах SQLite у режимі WAL,
а не на моках: пастка з чужим `-wal` на моку не відтворюється.
"""
from __future__ import annotations

import os
import socket
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from app import snapshot_cli
from app.snapshot_tools import (
    SnapshotError,
    discover_snapshots,
    inspect_snapshot,
    newest_valid,
    restore_from_snapshot,
)


def _make_db(path: Path, orders: int, *, revision: str = "0085_x") -> Path:
    con = sqlite3.connect(path)
    con.execute("create table orders(id integer primary key, note text)")
    con.execute("create table alembic_version(version_num text)")
    con.execute("insert into alembic_version values (?)", (revision,))
    con.executemany("insert into orders(note) values (?)", [("n" * 50,)] * orders)
    con.commit()
    con.close()
    return path


def _live_with_wal(path: Path, orders: int) -> sqlite3.Connection:
    """Жива база в WAL, транзакції якої ЛИШЕ в -wal (автоконтрольна точка вимкнена)."""
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("create table if not exists orders(id integer primary key, note text)")
    con.execute("begin")
    for _ in range(orders):
        con.execute("insert into orders(note) values ('live')")
    con.execute("commit")
    return con


def _not_running() -> bool:
    return False


def test_inspect_reads_a_good_snapshot_and_flags_a_bad_one(tmp_path):
    good = _make_db(tmp_path / "good.db", 7)
    info = inspect_snapshot(good)
    assert info.ok and info.orders == 7 and info.revision == "0085_x"

    bad = tmp_path / "bad.db"
    bad.write_bytes(os.urandom(5000))
    assert not inspect_snapshot(bad).ok

    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert not inspect_snapshot(empty).ok

    foreign = tmp_path / "foreign.db"
    con = sqlite3.connect(foreign)
    con.execute("create table something(x)")
    con.commit()
    con.close()
    assert "не база KuubMill" in inspect_snapshot(foreign).detail


def test_inspect_leaves_no_side_files_next_to_the_snapshot(tmp_path):
    """-wal/-shm біля знімка — пастка відновлення; перевірка їх не заводить."""
    snap = _make_db(tmp_path / "s.db", 3)
    inspect_snapshot(snap)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.db"]


def test_restore_replaces_db_and_keeps_the_old_one_aside(tmp_path):
    live = tmp_path / "kuubmill.db"
    _make_db(live, 5)
    snap = _make_db(tmp_path / "snap.db", 40)

    report = restore_from_snapshot(live, snap, running=_not_running, now=datetime(2026, 10, 7, 12, 0, 0))

    assert report["ok"] and report["orders"] == 40
    assert inspect_snapshot(live).orders == 40
    kept = Path(report["kept_aside"][0])
    assert kept.name == "kuubmill.db.before-restore-20261007-120000"
    assert inspect_snapshot(kept).orders == 5, "стара база мусить лишитись цілою"


def _crash_leaving_a_wal(live: Path, orders: int) -> Path:
    """Жива база з незакритим WAL, як після аварійного завершення процесу.

    Звичайне закриття з'єднання саме зливає й видаляє -wal, тож знімаємо його
    копію ДО закриття й кладемо назад: на диску лишаються .db + чужий непорожній -wal."""
    keeper = _live_with_wal(live, orders)
    wal = Path(str(live) + "-wal")
    saved = live.with_name("wal.saved")
    saved.write_bytes(wal.read_bytes())
    keeper.close()
    wal.write_bytes(saved.read_bytes())
    saved.unlink()
    assert wal.stat().st_size > 0, "передумова: у живої бази лишився непорожній -wal"
    return wal


def test_naive_file_swap_with_a_stale_wal_is_unsafe(tmp_path):
    """Контроль: чому цей інструмент існує. Підмінити лише .db при чужому -wal —
    SQLite накочує чужі кадри на знімок (навчання 07.10.26: «malformed»)."""
    import shutil

    live = tmp_path / "kuubmill.db"
    _crash_leaving_a_wal(live, 2000)
    snap = _make_db(tmp_path / "snap.db", 40)
    shutil.copyfile(snap, live)  # «відновлення руками»
    try:
        con = sqlite3.connect(live)
        ok = con.execute("pragma integrity_check").fetchone()[0] == "ok"
        n = con.execute("select count(*) from orders").fetchone()[0]
        con.close()
        damaged = (not ok) or n != 40
    except sqlite3.Error:
        damaged = True
    assert damaged, "якщо це більше не ламає — пастка зникла, і тест можна прибрати"


def test_restore_with_a_stale_wal_next_to_the_live_db(tmp_path):
    live = tmp_path / "kuubmill.db"
    wal = _crash_leaving_a_wal(live, 2000)
    snap = _make_db(tmp_path / "snap.db", 40)

    report = restore_from_snapshot(live, snap, running=_not_running)

    con = sqlite3.connect(live)
    assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert con.execute("select count(*) from orders").fetchone()[0] == 40
    con.close()
    assert any(p.endswith("-wal" + p[p.index(".before-restore"):]) for p in report["kept_aside"]), \
        "чужий -wal мусить бути відкладений, а не лишитись поруч"
    assert not wal.exists() or wal.stat().st_size == 0

def test_restore_refuses_a_corrupt_snapshot_and_changes_nothing(tmp_path):
    live = tmp_path / "kuubmill.db"
    _make_db(live, 5)
    bad = tmp_path / "bad.db"
    bad.write_bytes(os.urandom(8000))

    with pytest.raises(SnapshotError, match="непридатний"):
        restore_from_snapshot(live, bad, running=_not_running)

    assert inspect_snapshot(live).orders == 5
    assert [p.name for p in tmp_path.iterdir() if "before-restore" in p.name or "restore-tmp" in p.name] == []


def test_restore_refuses_while_the_app_is_running(tmp_path):
    live = tmp_path / "kuubmill.db"
    _make_db(live, 5)
    snap = _make_db(tmp_path / "snap.db", 40)
    with pytest.raises(SnapshotError, match="працює"):
        restore_from_snapshot(live, snap, running=lambda: True)
    assert inspect_snapshot(live).orders == 5


def test_app_is_running_sees_a_listening_port():
    from app.snapshot_tools import app_is_running

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert app_is_running(port)
    assert not app_is_running(port)


def test_restore_when_there_is_no_live_db_at_all(tmp_path):
    """Новий ПК: бази ще нема — відновлення просто ставить знімок."""
    live = tmp_path / "new" / "kuubmill.db"
    snap = _make_db(tmp_path / "snap.db", 12)
    report = restore_from_snapshot(live, snap, running=_not_running)
    assert report["orders"] == 12 and report["kept_aside"] == []


def test_discover_skips_temp_and_aside_files_and_orders_newest_first(tmp_path):
    backups = tmp_path / "backups"
    (backups / "monthly").mkdir(parents=True)
    old = _make_db(backups / "monthly" / "kuubmill-2026-08.db", 1)
    new = _make_db(backups / "kuubmill_20261005_101054.db", 2)
    _make_db(backups / "kuubmill.db.before-restore-20261001-000000", 3)
    _make_db(backups / "x.db.tmp", 4)
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))

    found = discover_snapshots(tmp_path)

    assert [s.path.name for s in found] == ["kuubmill_20261005_101054.db", "kuubmill-2026-08.db"]
    assert newest_valid(found).orders == 2


def test_discover_includes_extra_dirs_like_a_mirror_disk(tmp_path):
    mirror = tmp_path / "usb"
    mirror.mkdir()
    _make_db(mirror / "kuubmill-2026-09.db", 9)
    found = discover_snapshots(tmp_path / "data", extra_dirs=[mirror])
    assert [s.orders for s in found] == [9]


def test_cli_restores_latest_valid_snapshot_after_confirmation(tmp_path):
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    live = data / "kuubmill.db"
    _make_db(live, 1)
    broken = data / "backups" / "kuubmill_20261006_000000.db"
    broken.write_bytes(os.urandom(4000))
    os.utime(broken, (1_900_000_000, 1_900_000_000))  # найновіший, але пошкоджений
    good = _make_db(data / "backups" / "kuubmill_20261005_000000.db", 33)
    os.utime(good, (1_800_000_000, 1_800_000_000))
    said: list[str] = []
    asked: list[str] = []

    rc = snapshot_cli.run(
        list_only=False, target="latest", data_dir=data, db_file=live,
        say=said.append, ask=lambda t: asked.append(t) or True, running=_not_running,
    )

    assert rc == 0 and inspect_snapshot(live).orders == 33, "пошкоджений найновіший мав бути пропущений"
    assert "kuubmill_20261005_000000.db" in asked[0]
    assert "Відновлено 33" in said[-1]


def test_cli_does_nothing_when_the_person_says_no(tmp_path):
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    live = data / "kuubmill.db"
    _make_db(live, 1)
    _make_db(data / "backups" / "kuubmill_20261005_000000.db", 33)
    rc = snapshot_cli.run(
        list_only=False, target=None, data_dir=data, db_file=live,
        say=lambda t: None, ask=lambda t: False, running=_not_running,
    )
    assert rc == 1 and inspect_snapshot(live).orders == 1


# ── Сторож бази на старті (рішення власника 07.10.26) ─────────────────────────


def _data_with_snapshot(tmp_path, snap_orders=33):
    data = tmp_path / "data"
    (data / "backups").mkdir(parents=True)
    snap = _make_db(data / "backups" / "kuubmill_20261005_000000.db", snap_orders)
    return data, data / "kuubmill.db", snap


def _guard(db, data, *, answer=True):
    said: list[str] = []
    asked: list[str] = []
    result = snapshot_cli.guard_database(
        db_file=db, data_dir=data, say=said.append, ask=lambda t: asked.append(t) or answer,
    )
    return result, said, asked


@pytest.mark.parametrize("damage", ["missing", "zero", "garbage", "truncated", "header_zeroed", "page_corrupt"])
def test_guard_offers_the_snapshot_for_every_kind_of_damage_and_restores_on_yes(tmp_path, damage):
    data, live, _ = _data_with_snapshot(tmp_path)
    if damage != "missing":
        _make_db(live, 500)
        raw = live.read_bytes()
        if damage == "zero":
            live.write_bytes(b"")
        elif damage == "garbage":
            live.write_bytes(os.urandom(6000))
        elif damage == "truncated":
            live.write_bytes(raw[: len(raw) // 2])
        elif damage == "header_zeroed":
            live.write_bytes(b"\x00" * 100 + raw[100:])
        elif damage == "page_corrupt":
            probe = sqlite3.connect(live)
            ps = probe.execute("pragma page_size").fetchone()[0]
            root = probe.execute("select rootpage from sqlite_master where name='orders'").fetchone()[0]
            probe.close()  # відкритий дескриптор не дав би Windows перейменувати файл
            with open(live, "r+b") as fh:
                fh.seek((root - 1) * ps + 20)
                fh.write(os.urandom(ps - 40))

    result, said, asked = _guard(live, data)

    assert result == "restored", (damage, said)
    assert "kuubmill_20261005_000000.db" in asked[0]
    assert inspect_snapshot(live).orders == 33
    if damage != "missing":
        assert any("before-restore" in p.name for p in data.iterdir()), "стару базу мусить бути збережено поруч"


def test_guard_declined_changes_nothing(tmp_path):
    data, live, _ = _data_with_snapshot(tmp_path)
    live.write_bytes(os.urandom(6000))
    before = live.read_bytes()

    result, _, asked = _guard(live, data, answer=False)

    assert result == "declined" and asked
    assert live.read_bytes() == before
    assert [p.name for p in data.iterdir()] == ["backups", "kuubmill.db"]


def test_guard_lets_a_first_install_through_without_asking(tmp_path):
    """Бази нема й знімків нема — це перша інсталяція, не аварія."""
    data = tmp_path / "data"
    data.mkdir()
    result, said, asked = _guard(data / "kuubmill.db", data)
    assert result == "ok" and not asked and not said


def test_guard_does_not_ask_about_a_healthy_database(tmp_path):
    data, live, _ = _data_with_snapshot(tmp_path)
    _make_db(live, 500)
    result, _, asked = _guard(live, data)
    assert result == "ok" and not asked and inspect_snapshot(live).orders == 500


def test_guard_stops_loudly_when_the_database_is_broken_and_no_snapshot_exists(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    live = data / "kuubmill.db"
    live.write_bytes(os.urandom(6000))
    result, said, asked = _guard(live, data)
    assert result == "declined" and not asked
    assert "не знайдено" in said[0] and "--restore-snapshot" in said[0]


def test_guard_never_restores_over_a_database_that_is_merely_locked(tmp_path, monkeypatch):
    """Заблокована база — не доказ пошкодження. Помилкове відновлення затерло б живі дані."""
    data, live, _ = _data_with_snapshot(tmp_path)
    _make_db(live, 500)
    holder = sqlite3.connect(live, isolation_level=None)
    holder.execute("begin exclusive")
    try:
        result, _, asked = _guard(live, data)
        assert result == "ok" and not asked
    finally:
        holder.execute("rollback")
        holder.close()
    assert inspect_snapshot(live).orders == 500


def test_database_state_reads_a_live_wal_database_without_losing_its_wal(tmp_path):
    """Жива база з -wal читається звичайним відкриттям (не immutable) — інакше свіжі
    транзакції лишилися б непоміченими."""
    from app.snapshot_tools import database_state

    live = tmp_path / "kuubmill.db"
    keeper = _live_with_wal(live, 50)
    try:
        assert database_state(live)[0] == "ok"
    finally:
        keeper.close()


def test_cli_lists_and_reports_when_there_are_no_snapshots(tmp_path):
    said: list[str] = []
    rc = snapshot_cli.run(list_only=True, target=None, data_dir=tmp_path, db_file=tmp_path / "kuubmill.db",
                          say=said.append, ask=lambda t: False)
    assert rc == 1 and "не знайдено" in said[0]
