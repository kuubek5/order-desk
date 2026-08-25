"""Entry point for the packaged standalone Windows application."""

import argparse
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import socket
import threading
import time
import urllib.request
import webbrowser

from app.runtime import data_dir, resource_path

APP_URL = "http://127.0.0.1:8000"
MUTEX_NAME = "Local\\OrderDeskStandalone"
SHUTDOWN_EVENT_NAME = "Local\\OrderDeskShutdown"
DATA_DIR = data_dir()
DB_PATH = str(DATA_DIR / "order_desk.db")
_mutex_handle = None
_shutdown_event_handle = None


def _already_running() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex_handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not _mutex_handle:
        raise OSError("Не вдалося створити Windows mutex")
    return kernel32.GetLastError() == 183


def _create_shutdown_event() -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    global _shutdown_event_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateEventW.restype = wintypes.HANDLE
    _shutdown_event_handle = kernel32.CreateEventW(
        None, True, False, SHUTDOWN_EVENT_NAME
    )
    if not _shutdown_event_handle:
        raise OSError("Не вдалося створити Windows shutdown event")


def _signal_shutdown(timeout_seconds: int = 15) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    mutex_handle = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)
    if not mutex_handle:
        return True
    kernel32.OpenEventW.restype = wintypes.HANDLE
    event_handle = None
    try:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            event_handle = kernel32.OpenEventW(0x0002, False, SHUTDOWN_EVENT_NAME)
            if event_handle:
                break
            # The process may exit while still in migrations, before creating
            # its shutdown event.
            if kernel32.WaitForSingleObject(mutex_handle, 0) in (0x00000000, 0x00000080):
                return True
            time.sleep(0.25)
        if not event_handle:
            return False
        if not kernel32.SetEvent(event_handle):
            return False
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        wait_result = kernel32.WaitForSingleObject(mutex_handle, remaining_ms)
        return wait_result in (0x00000000, 0x00000080)
    finally:
        kernel32.CloseHandle(mutex_handle)
        if event_handle:
            kernel32.CloseHandle(event_handle)


def _configure_logging() -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "order-desk.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


def _show_startup_error() -> None:
    if os.name != "nt":
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        0,
        f"Order Desk не вдалося запустити.\n\nДеталі: {DATA_DIR / 'logs' / 'order-desk.log'}",
        "Order Desk",
        0x10,
    )


def _ensure_port_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError as exc:
            raise RuntimeError("Локальний порт 8000 уже зайнятий іншою програмою") from exc


def _open_browser_when_ready() -> None:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"{APP_URL}/health", timeout=1):
                webbrowser.open(APP_URL)
                return
        except OSError:
            time.sleep(0.2)


def _backup_database(db_file: Path) -> Path | None:
    if not db_file.is_file() or db_file.stat().st_size == 0:
        return None
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"order_desk_{datetime.now():%Y%m%d_%H%M%S}.db"
    shutil.copy2(db_file, destination)
    backups = sorted(backup_dir.glob("order_desk_*.db"), reverse=True)
    for old_backup in backups[5:]:
        old_backup.unlink(missing_ok=True)
    return destination


def _alembic_config():
    from alembic.config import Config

    config = Config(str(resource_path("alembic.ini")))
    config.set_main_option(
        "script_location", str(resource_path("migrations")).replace("%", "%%")
    )
    return config


def _run_migrations() -> None:
    from alembic import command
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, URL

    from scripts.migration_guard import main as migration_guard

    db_file = Path(DB_PATH).expanduser().resolve()
    os.environ["DB_PATH"] = str(db_file)
    guard_status = migration_guard()
    if guard_status == 4:
        raise RuntimeError(
            "Схема локальної бази несумісна з цією версією. "
            "Запуск зупинено, дані не змінені."
        )

    config = _alembic_config()
    if guard_status == 3:
        backup = _backup_database(db_file)
        logging.info("Legacy database backup created: %s", backup)
        command.stamp(config, "0001_initial")

    current_revision = None
    if db_file.exists() and db_file.stat().st_size:
        engine = create_engine(URL.create("sqlite", database=str(db_file)))
        with engine.connect() as connection:
            current_revision = MigrationContext.configure(connection).get_current_revision()
        engine.dispose()

    head_revision = ScriptDirectory.from_config(config).get_current_head()
    if current_revision != head_revision:
        backup = _backup_database(db_file)
        if backup:
            logging.info("Pre-migration backup created: %s", backup)
        command.upgrade(config, "head")


def _load_tray_image():
    """The bundled brand .ico as a PIL image for the tray, or None if anything
    is missing — a tray is a nicety and must never keep the app from starting."""
    try:
        from PIL import Image

        return Image.open(resource_path("assets/orderdesk.ico"))
    except Exception:
        logging.exception("Tray icon image unavailable")
        return None


def _run_server_with_tray(server, tray_holder: dict) -> None:
    """Run uvicorn under a system-tray icon: server on a background thread, the
    tray's message loop on the main thread. Right-click gives Open / pause the
    sheet sync / Quit. Any failure to build the tray falls back to running the
    server directly (current behaviour), so packaging or platform issues never
    brick the app — the web UI stays fully usable without the tray."""
    image = None
    icon = None
    # Headless/non-interactive (CI smoke test, service context): no desktop to
    # host a tray, so skip it outright and just serve. Keeps the smoke test
    # deterministic instead of relying on the tray-failure fallback below.
    if os.name == "nt" and not os.environ.get("ORDER_DESK_NONINTERACTIVE"):
        try:
            import pystray

            image = _load_tray_image()
            if image is not None:
                from app import sync_control

                def _open(_icon=None, _item=None) -> None:
                    webbrowser.open(APP_URL)

                def _toggle_pause(_icon=None, _item=None) -> None:
                    sync_control.set_paused(not sync_control.is_paused())
                    if icon is not None:
                        icon.update_menu()

                def _is_paused(_item=None) -> bool:
                    return sync_control.is_paused()

                def _quit(_icon=None, _item=None) -> None:
                    server.should_exit = True
                    if icon is not None:
                        icon.stop()

                menu = pystray.Menu(
                    pystray.MenuItem("Відкрити Order Desk", _open, default=True),
                    pystray.MenuItem(
                        "Синхронізація таблиці на паузі",
                        _toggle_pause,
                        checked=_is_paused,
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Вийти", _quit),
                )
                icon = pystray.Icon("OrderDesk", image, "Order Desk", menu)
                tray_holder["icon"] = icon
        except Exception:
            logging.exception("Tray unavailable — running without it")
            icon = None

    if icon is None:
        server.run()
        return

    server_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    server_thread.start()
    try:
        icon.run()  # blocks on the main thread until icon.stop()
    except Exception:
        # The tray loop itself failed (e.g. a headless CI runner with no usable
        # window station). The app must KEEP SERVING — the web UI is the product,
        # the tray is a convenience. Wait on the server instead of tearing it
        # down; an external --shutdown still stops it via watch_shutdown.
        logging.exception("Tray loop failed — continuing to serve without it")
        server_thread.join()
        return
    # Tray ended normally (Quit or the shutdown event) — wind the server down.
    server.should_exit = True
    server_thread.join(timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--shutdown", action="store_true")
    args, _ = parser.parse_known_args()

    if args.shutdown:
        return 0 if _signal_shutdown() else 1

    if _already_running():
        webbrowser.open(APP_URL)
        return 0

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _configure_logging()
        from app.config import DATA_DIR as configured_data_dir, DB_PATH as configured_db_path

        global DB_PATH
        DB_PATH = configured_db_path
        if configured_data_dir != DATA_DIR:
            raise RuntimeError("Неузгоджений Windows data directory")
        _ensure_port_available()
        _run_migrations()
        # Alembic's logging config targets stderr; restore the rotating file
        # handler required by the no-console Windows build.
        _configure_logging()
        os.environ["ORDER_DESK_SCHEMA_MANAGED"] = "1"
        from app.web import app
        import uvicorn

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=8000,
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=10,
        )
        server = uvicorn.Server(config)
        _create_shutdown_event()

        # The tray icon (if available) needs the main thread for its Windows
        # message loop, so it holds a reference to the icon to stop on shutdown.
        tray_holder: dict = {}

        def watch_shutdown() -> None:
            if os.name != "nt" or not _shutdown_event_handle:
                return
            import ctypes

            ctypes.windll.kernel32.WaitForSingleObject(_shutdown_event_handle, 0xFFFFFFFF)
            server.should_exit = True
            icon = tray_holder.get("icon")
            if icon is not None:
                try:
                    icon.stop()
                except Exception:
                    logging.exception("Tray icon stop failed on shutdown")

        threading.Thread(target=watch_shutdown, daemon=True).start()
        if args.open_browser:
            threading.Thread(target=_open_browser_when_ready, daemon=True).start()
        _run_server_with_tray(server, tray_holder)
        return 0
    except (Exception, SystemExit):
        import traceback

        try:
            _configure_logging()
            logging.exception("Order Desk failed to start")
        except Exception:
            pass
        # Standalone traceback file that never depends on logging being set up —
        # the packaged build is windowed (no stderr), so without this a startup
        # crash leaves no trace at all on a headless/CI host.
        try:
            err_dir = DATA_DIR / "logs"
            err_dir.mkdir(parents=True, exist_ok=True)
            (err_dir / "startup-error.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        # A modal MessageBox blocks forever on a non-interactive host (no one to
        # dismiss it) — which is exactly what hung the release smoke test and hid
        # the real error. Skip it when there's no desktop to show it on.
        if not os.environ.get("ORDER_DESK_NONINTERACTIVE"):
            _show_startup_error()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
