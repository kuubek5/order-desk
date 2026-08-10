from pathlib import Path
import sqlite3

from alembic.config import Config
from alembic import command

from scripts.migration_guard import main

_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _build_legacy_db(db_path: Path, monkeypatch) -> None:
    """Builds a DB matching the frozen 0001_initial migration — NOT the
    live app/models.py, which will keep growing new columns over time.
    EXPECTED_COLUMNS in scripts/migration_guard.py is a historical snapshot
    of what a real pre-Alembic production database looked like, so the test
    for "legacy schema matches baseline" must be built from that same frozen
    migration script, or it silently breaks the first time any post-baseline
    column (e.g. email_messages.service_type_guess, added in 0002) is added
    to the live models.
    """
    monkeypatch.setenv("DB_PATH", str(db_path))
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_INI.parent / "migrations"))
    command.upgrade(config, "0001_initial")
    # The guard treats an `alembic_version` table as "already Alembic-managed"
    # (returns 0 early) — drop it so this DB still looks like the unversioned
    # legacy database it's meant to simulate.
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE alembic_version")
    connection.commit()
    connection.close()


def test_new_database_is_safe_for_upgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "new.db"))
    assert main() == 0


def test_matching_legacy_schema_requires_explicit_stamp(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(db_path, monkeypatch)

    assert main() == 3


def test_incompatible_legacy_schema_is_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "wrong.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    monkeypatch.setenv("DB_PATH", str(db_path))

    assert main() == 4


def test_alembic_managed_database_is_safe_for_upgrade(tmp_path, monkeypatch):
    db_path = tmp_path / "managed.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    connection.commit()
    connection.close()
    monkeypatch.setenv("DB_PATH", str(db_path))

    assert main() == 0
