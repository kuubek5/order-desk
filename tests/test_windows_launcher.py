import os
from pathlib import Path

import app.windows_launcher as launcher


def test_backup_database_copies_db_and_keeps_five(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "kuubmill.db"
    db_file.write_bytes(b"sqlite-data")
    backup_dir = data_dir / "backups"
    backup_dir.mkdir()
    # Копії з часів назви Order Desk: ротація мусить рахувати їх разом з новими,
    # інакше вони лишаються на диску назавжди.
    for index in range(6):
        old = backup_dir / f"order_desk_2026010{index + 1}_120000.db"
        old.write_bytes(str(index).encode())
        os.utime(old, (1_700_000_000 + index, 1_700_000_000 + index))

    monkeypatch.setattr(launcher, "DATA_DIR", data_dir)
    created = launcher._backup_database(db_file)

    assert created is not None
    assert created.read_bytes() == b"sqlite-data"
    kept = [*backup_dir.glob("kuubmill_*.db"), *backup_dir.glob("order_desk_*.db")]
    assert len(kept) == 5
    # Свіжа копія мусить пережити ротацію: сортування за іменем ставило б її
    # після «order_desk_» і видаляло б першою.
    assert created.exists()


def test_backup_database_ignores_missing_or_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path)
    missing = tmp_path / "missing.db"
    empty = tmp_path / "empty.db"
    empty.touch()

    assert launcher._backup_database(missing) is None
    assert launcher._backup_database(empty) is None


def test_installer_stops_running_app_before_replacing_files():
    script = (Path(__file__).parents[1] / "installer" / "KuubMill.iss").read_text(
        encoding="utf-8"
    )

    assert "function PrepareToInstall" in script
    assert "Exec(ExistingExe, '--shutdown'" in script
    assert "ewWaitUntilTerminated" in script
