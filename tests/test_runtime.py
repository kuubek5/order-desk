from pathlib import Path

import app.runtime as runtime


def test_data_dir_override_is_absolute(tmp_path, monkeypatch):
    requested = tmp_path / "nested" / ".." / "order-desk-data"
    monkeypatch.setenv("ORDER_DESK_DATA_DIR", str(requested))

    assert runtime.data_dir() == requested.resolve()


def test_frozen_windows_data_dir_uses_local_app_data(tmp_path, monkeypatch):
    monkeypatch.delenv("ORDER_DESK_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)

    assert runtime.data_dir() == tmp_path / "OrderDesk"


def test_source_resource_path_is_rooted_at_project(monkeypatch):
    monkeypatch.delattr(runtime.sys, "_MEIPASS", raising=False)

    expected_root = Path(runtime.__file__).resolve().parents[1]
    assert runtime.resource_path("app/templates") == expected_root / "app/templates"


def test_frozen_resource_path_is_rooted_at_meipass(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert runtime.resource_path("app/static") == tmp_path / "app/static"


def test_sqlite_engine_uses_an_absolute_database_path():
    from app.db import db_file, engine

    assert db_file.is_absolute()
    assert engine.url.drivername == "sqlite"
    assert engine.url.database == str(db_file)


def test_sqlite_engine_enables_wal_and_busy_timeout():
    from app.db import engine

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000
