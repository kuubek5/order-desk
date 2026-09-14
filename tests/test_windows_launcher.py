import os
from pathlib import Path

import pytest

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


# ── Трей: «Вийти» ──────────────────────────────────────────────────────────


def test_quit_asks_off_the_tray_thread():
    """Питання НЕ має виконуватись у самому обробнику.

    Пункт меню pystray викликається на потоці циклу повідомлень трею, а
    `MessageBoxW` модальний: поки він відкритий, іконка не відповідає, і це
    читається як зависання застосунку. Тому обробник лише ЗАПУСКАЄ питання й
    одразу повертає керування.
    """
    from app.windows_launcher import make_quit_handler

    started: list = []
    stopped: list = []
    quit_item = make_quit_handler(
        lambda: stopped.append(True),
        confirm=lambda: True,
        spawn=lambda fn: started.append(fn),
    )

    quit_item()
    assert len(started) == 1, "обробник мусить віддати питання окремому виконавцю"
    assert stopped == [], "до відповіді застосунок не зупиняється"

    started[0]()  # людина натиснула «Так»
    assert stopped == [True]


def test_second_click_while_asking_does_not_open_a_second_dialog():
    """Кадр із цеху 15.09.26: два однакові вікна «Вийти з KuubMill?» одне
    поверх одного. У модального вікна власний цикл повідомлень, тож клік по
    треї доходить удруге. Повторний вхід мусить ігноруватись."""
    from app.windows_launcher import make_quit_handler

    started: list = []
    quit_item = make_quit_handler(
        lambda: None, confirm=lambda: True, spawn=lambda fn: started.append(fn)
    )

    quit_item()
    quit_item()
    quit_item()
    assert len(started) == 1, "поки питання відкрите, нові кліки не плодять вікон"

    started[0]()  # відповіли — замок знято
    quit_item()
    assert len(started) == 2


def test_refusing_to_quit_leaves_the_app_running_and_unlocks():
    from app.windows_launcher import make_quit_handler

    started: list = []
    stopped: list = []
    quit_item = make_quit_handler(
        lambda: stopped.append(True),
        confirm=lambda: False,
        spawn=lambda fn: started.append(fn),
    )
    quit_item()
    started[0]()
    assert stopped == [], "«Ні» не зупиняє застосунок"

    quit_item()
    assert len(started) == 2, "після відмови пункт меню знову робочий"


def test_a_failing_dialog_does_not_wedge_the_menu_forever():
    """Якщо саме питання впало, замок мусить зніматись — інакше пункт «Вийти»
    більше ніколи не спрацює, і вийти можна буде лише вбивши процес."""
    from app.windows_launcher import make_quit_handler

    started: list = []
    quit_item = make_quit_handler(
        lambda: None,
        confirm=lambda: (_ for _ in ()).throw(RuntimeError("немає робочого столу")),
        spawn=lambda fn: started.append(fn),
    )
    quit_item()
    with pytest.raises(RuntimeError):
        started[0]()

    quit_item()
    assert len(started) == 2
