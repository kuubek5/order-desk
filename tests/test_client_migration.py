"""Confirms migration 0004_add_client_table applies (and reverses) cleanly,
independent of the live app/models.py — same spirit as
tests/test_migration_guard.py's legacy-schema builder, but walking every
migration up to head instead of stopping at 0001_initial.
"""

from pathlib import Path
import sqlite3

from alembic.config import Config
from alembic import command


_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

EXPECTED_CLIENT_COLUMNS = {"id", "canonical_name", "phone", "email", "notes", "created_at"}


def _config() -> Config:
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_ALEMBIC_INI.parent / "migrations"))
    return config


def test_upgrade_head_creates_clients_table(tmp_path, monkeypatch):
    db_path = tmp_path / "head.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    command.upgrade(_config(), "head")

    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert "clients" in tables

        columns = {row[1] for row in connection.execute('PRAGMA table_info("clients")')}
        assert columns == EXPECTED_CLIENT_COLUMNS
    finally:
        connection.close()


def test_downgrade_from_head_drops_clients_table(tmp_path, monkeypatch):
    db_path = tmp_path / "downgrade.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, "0003_add_email_attachments_status")

    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert "clients" not in tables
    finally:
        connection.close()


def test_upgrade_from_0003_to_head_is_purely_additive(tmp_path, monkeypatch):
    """Upgrading an existing pre-clients DB must not touch any other table."""
    db_path = tmp_path / "existing.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    config = _config()
    command.upgrade(config, "0003_add_email_attachments_status")

    connection = sqlite3.connect(db_path)
    tables_before = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(db_path)
    try:
        tables_after = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        # 0004 adds clients; 0005 adds the material catalog; 0009 adds mail
        # filter rules; 0021 adds the operator action log; 0023 adds the shift
        # handover notes and their screenshots; 0024 adds the operator's
        # working set. All purely additive — nothing that existed at 0003 is
        # dropped.
        assert tables_after - tables_before == {
            "clients", "materials", "material_aliases",
            "mail_filter_rules", "mail_filter_categories", "client_sender_memory",
            "action_log", "shift_notes", "shift_note_images", "order_focus",
        }
        assert tables_before - tables_after == set()
    finally:
        connection.close()
