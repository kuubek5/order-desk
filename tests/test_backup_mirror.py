"""app/backup_mirror.py — друга копія знімка бази на іншому носії.

Головне, що тут стережеться: дзеркало НІКОЛИ не ламає основну копію. Мертва
мережева шара — нормальний стан, а не збій застосунку (аудит 08.09.26).
"""

import sqlite3
from pathlib import Path

import pytest

from app.backup_mirror import (
    MIRROR_DIR_KEY,
    mirror_enabled,
    mirror_snapshot,
    mirror_status,
)
from app.settings_store import set_setting


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('робота')")
    conn.commit()
    conn.close()
    return path


def test_disabled_by_default(db_session):
    assert mirror_enabled(db_session) is False
    snapshot = _make_db(Path("dummy.db"))
    try:
        assert mirror_snapshot(db_session, snapshot) is None
    finally:
        snapshot.unlink(missing_ok=True)


def test_copies_and_verifies(db_session, tmp_path):
    snapshot = _make_db(tmp_path / "kuubmill-2026-08.db")
    target = tmp_path / "mirror"
    set_setting(db_session, MIRROR_DIR_KEY, str(target))

    assert mirror_snapshot(db_session, snapshot, subdir="monthly") is None

    copied = target / "monthly" / snapshot.name
    assert copied.exists()
    # Копія читається як справжня база, не як набір байтів потрібного розміру.
    conn = sqlite3.connect(copied)
    try:
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "робота"
    finally:
        conn.close()
    # Жодного .tmp після успіху.
    assert not list((target / "monthly").glob("*.tmp"))

    status = mirror_status(db_session)
    assert status["last_ok"]
    assert status["last_error"] == ""


def test_unreachable_target_does_not_raise(db_session, tmp_path):
    """Мертва шара не має перетворювати вдалу основну копію на збій."""
    snapshot = _make_db(tmp_path / "kuubmill-2026-08.db")
    # Файл замість теки — mkdir на ньому гарантовано впаде.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    set_setting(db_session, MIRROR_DIR_KEY, str(blocker / "inner"))

    problem = mirror_snapshot(db_session, snapshot)

    assert problem is not None
    assert mirror_status(db_session)["last_error"]


def test_corrupt_copy_is_rejected_and_not_left_in_place(db_session, tmp_path, monkeypatch):
    """Биту копію не можна лишати: вона виглядає як копія й підводить саме тоді,
    коли з неї доведеться відновлюватись."""
    snapshot = _make_db(tmp_path / "kuubmill-2026-08.db")
    target = tmp_path / "mirror"
    set_setting(db_session, MIRROR_DIR_KEY, str(target))

    import app.backup_mirror as backup_mirror

    def _broken_copy(src, dst, *a, **kw):
        Path(dst).write_bytes(b"not a database at all")
        return dst

    monkeypatch.setattr(backup_mirror.shutil, "copy2", _broken_copy)

    problem = mirror_snapshot(db_session, snapshot)

    assert problem is not None
    assert not (target / snapshot.name).exists()
    assert not list(target.glob("*.tmp"))


def test_success_clears_previous_error(db_session, tmp_path):
    snapshot = _make_db(tmp_path / "kuubmill-2026-08.db")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    set_setting(db_session, MIRROR_DIR_KEY, str(blocker / "inner"))
    mirror_snapshot(db_session, snapshot)
    assert mirror_status(db_session)["last_error"]

    set_setting(db_session, MIRROR_DIR_KEY, str(tmp_path / "mirror"))
    assert mirror_snapshot(db_session, snapshot) is None
    assert mirror_status(db_session)["last_error"] == ""


def test_verification_leaves_no_side_files(db_session, tmp_path):
    """Перевірка копії не має лишати біля неї `-wal` і `-shm`.

    Знайдено заміром на живих мережевих шарах 08.09.26. З `mode=ro` SQLite
    вважає, що база може змінитись, і заводить супутні файли — у теці дзеркала
    вони лишались назавжди. Це не лише сміття: випадковий `-wal` біля `.db`
    змушує SQLite вважати базу незавершеною, тобто ламає саме те відновлення,
    заради якого копія й існує.
    """
    source = _make_db(tmp_path / "kuubmill-2026-08.db")
    # База в режимі WAL — саме такий випадок і створював супутні файли.
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO t (v) VALUES ('ще робота')")
        conn.commit()
    finally:
        conn.close()

    target = tmp_path / "mirror"
    set_setting(db_session, MIRROR_DIR_KEY, str(target))

    assert mirror_snapshot(db_session, source, subdir="pre-update") is None

    folder = target / "pre-update"
    leftovers = sorted(p.name for p in folder.iterdir() if p.suffix != ".db")
    assert leftovers == [], f"перевірка лишила сміття поруч із копією: {leftovers}"
    assert (folder / source.name).exists()


def test_pre_update_snapshot_is_mirrored_too():
    """Копія перед оновленням — єдине, з чого відкочуватись, якщо нова версія
    не піде. Лежати поруч із базою, яку вона страхує, для неї найгірша адреса."""
    import inspect

    from app.routers.settings import update as update_router

    source = inspect.getsource(update_router.install_update)
    assert "mirror_snapshot" in source, (
        "знімок перед оновленням не дзеркалиться — при смерті диска зникне "
        "разом із базою"
    )
