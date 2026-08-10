"""Classify a SQLite DB before automatic Alembic migrations.

Exit codes:
  0: empty/new or already Alembic-managed; automatic upgrade is safe
  3: unversioned legacy schema matches the baseline; explicit stamp required
  4: unversioned schema does not match the baseline; manual review required
"""

import os
from pathlib import Path
import sqlite3
import sys


EXPECTED_COLUMNS = {
    "app_settings": {"key", "value_encrypted", "updated_at"},
    "attachments": {"id", "email_message_id", "filename", "saved_path", "size_bytes", "created_at"},
    "client_name_aliases": {"id", "sheet_name", "export_folder_name", "confirmed", "confirmed_at"},
    "comments": {"id", "order_id", "source", "author", "text", "created_at", "synced_at"},
    "email_messages": {
        "id", "uid", "from_address", "subject", "body_text", "received_at",
        "client_name_guess", "material_color_guess", "kind_guess", "quantity_guess",
        "status", "order_id", "created_at",
    },
    "orders": {
        "id", "source", "sheet_tab", "row_number", "work_order_no", "job_code",
        "quantity", "material_color", "kind", "due_time", "technician_name",
        "cam_comment", "sum3d_id", "calculated_raw", "milled_raw",
        "last_milled_date", "mill_count", "client_name", "status", "created_at", "updated_at",
    },
    "rework_records": {
        "id", "order_id", "occurrence", "blame", "blame_quantity", "redo_quantity",
        "cam_comment", "sum3d_id", "calculated_raw", "milled_raw", "created_at",
    },
    "status_events": {"id", "order_id", "operator_id", "status", "actor", "note", "occurred_at"},
    "sync_logs": {"id", "direction", "sheet_tab", "status", "message", "occurred_at"},
    "users": {"id", "username", "password_hash", "full_name", "role", "is_active", "created_at"},
}


def main() -> int:
    db_path = Path(os.environ.get("DB_PATH", "order_desk.db"))
    if not db_path.exists() or db_path.stat().st_size == 0:
        print(f"Migration guard: new database at {db_path}")
        return 0

    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            print(f"Migration guard: empty database at {db_path}")
            return 0
        if "alembic_version" in tables:
            print(f"Migration guard: Alembic-managed database at {db_path}")
            return 0

        actual_tables = tables
        expected_tables = set(EXPECTED_COLUMNS)
        problems: list[str] = []
        if actual_tables != expected_tables:
            problems.append(
                f"tables differ (missing={sorted(expected_tables - actual_tables)}, "
                f"extra={sorted(actual_tables - expected_tables)})"
            )

        for table in sorted(actual_tables & expected_tables):
            quoted_table = table.replace('"', '""')
            columns = {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{quoted_table}")')
            }
            if columns != EXPECTED_COLUMNS[table]:
                problems.append(
                    f"{table} columns differ "
                    f"(missing={sorted(EXPECTED_COLUMNS[table] - columns)}, "
                    f"extra={sorted(columns - EXPECTED_COLUMNS[table])})"
                )
    finally:
        connection.close()

    if problems:
        print("Migration guard: REFUSING unversioned incompatible database:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 4

    print(
        "Migration guard: legacy schema matches baseline, but explicit backup and "
        "'alembic stamp 0001_initial' are required before startup.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
