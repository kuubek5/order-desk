from pathlib import Path

import app.windows_launcher as launcher


def test_backup_database_copies_db_and_keeps_five(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "order_desk.db"
    db_file.write_bytes(b"sqlite-data")
    backup_dir = data_dir / "backups"
    backup_dir.mkdir()
    for index in range(6):
        old = backup_dir / f"order_desk_2026010{index + 1}_120000.db"
        old.write_bytes(str(index).encode())

    monkeypatch.setattr(launcher, "DATA_DIR", data_dir)
    created = launcher._backup_database(db_file)

    assert created is not None
    assert created.read_bytes() == b"sqlite-data"
    assert len(list(backup_dir.glob("order_desk_*.db"))) == 5


def test_backup_database_ignores_missing_or_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path)
    missing = tmp_path / "missing.db"
    empty = tmp_path / "empty.db"
    empty.touch()

    assert launcher._backup_database(missing) is None
    assert launcher._backup_database(empty) is None


def test_installer_stops_running_app_before_replacing_files():
    script = (Path(__file__).parents[1] / "installer" / "OrderDesk.iss").read_text(
        encoding="utf-8"
    )

    assert "function PrepareToInstall" in script
    assert "Exec(ExistingExe, '--shutdown'" in script
    assert "ewWaitUntilTerminated" in script
