"""Entry point for the packaged standalone Windows application."""

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from app.runtime import LOG_FORMAT, data_dir, resource_path

APP_URL = "http://127.0.0.1:8000"
MUTEX_NAME = "Local\\KuubMillStandalone"
SHUTDOWN_EVENT_NAME = "Local\\KuubMillShutdown"
# Імена з часів назви Order Desk. Потрібні рівно на одне оновлення: інсталятор
# нової версії просить СТАРИЙ процес вийти, а той відгукується лише на старі
# імена. Без цього Windows показав би діалог про зайняті файли, і оновлення
# зупинилось би. Прибрати можна після того, як на всіх машинах уже стоїть
# збірка з новими іменами.
LEGACY_MUTEX_NAME = "Local\\Order" "DeskStandalone"
LEGACY_SHUTDOWN_EVENT_NAME = "Local\\Order" "DeskShutdown"
DATA_DIR = data_dir()
DB_PATH = str(DATA_DIR / "kuubmill.db")
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
    # Спершу шукаємо процес під новим іменем, потім під старим: під час
    # оновлення з версії до перейменування працює саме старий.
    mutex_handle = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)
    event_name = SHUTDOWN_EVENT_NAME
    if not mutex_handle:
        mutex_handle = kernel32.OpenMutexW(0x00100000, False, LEGACY_MUTEX_NAME)
        event_name = LEGACY_SHUTDOWN_EVENT_NAME
    if not mutex_handle:
        return True
    kernel32.OpenEventW.restype = wintypes.HANDLE
    event_handle = None
    try:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            event_handle = kernel32.OpenEventW(0x0002, False, event_name)
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
        log_dir / "kuubmill.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
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
        f"KuubMill не вдалося запустити.\n\nДеталі: {DATA_DIR / 'logs' / 'kuubmill.log'}",
        "KuubMill",
        0x10,
    )


def _ensure_port_available() -> None:
    # Проба лише на петлі, хоч слухати можемо й 0.0.0.0: Windows не дає
    # прив'язати 127.0.0.1:8000, коли хтось уже тримає 0.0.0.0:8000, тож
    # конфлікт видно в обох випадках, а база (де лежить перемикач) на цей
    # момент ще не мігрована.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError as exc:
            raise RuntimeError("Локальний порт 8000 уже зайнятий іншою програмою") from exc


def _startup_host() -> str:
    """Адреса для `uvicorn` за перемикачем «Робота з інших ПК»
    (`app/services/network_access.py`). Читається РАЗ, після міграцій. Будь-яка
    невдача читання = петля: помилка в базі не має відкривати порт у мережу."""
    from app.services.network_access import LOOPBACK_HOST, desired_host

    try:
        from app.db import SessionLocal

        with SessionLocal() as db:
            return desired_host(db)
    except Exception:
        logging.exception("Не вдалося прочитати перемикач «Робота з інших ПК» — слухаю лише петлю")
        return LOOPBACK_HOST


def _confirm_quit() -> bool:
    """Діалог «Вийти?» з треєм. Без робочого столу (тести, CI) — так."""
    if os.name != "nt" or os.environ.get("KUUBMILL_NONINTERACTIVE"):
        return True
    import ctypes

    text = (
        "Вийти з KuubMill?\n\n"
        "Застосунок зупиниться: синхронізація таблиці, пошта, печі й верстати "
        "перестануть оновлюватись, а відкриті екрани перестануть відповідати."
    )
    try:
        from app.services.network_access import network_listening

        if network_listening():
            text += "\n\nУвімкнено «Робота з інших ПК»: колеги за іншими ПК теж втратять доступ."
    except Exception:
        pass
    # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST
    flags = 0x4 | 0x30 | 0x100 | 0x10000 | 0x40000
    return ctypes.windll.user32.MessageBoxW(0, text, "KuubMill", flags) == 6  # IDYES


def relaunch_command(pid: int, exe: str, args: list[str]) -> list[str]:
    """Команда, що чекає виходу ЦЬОГО процесу й запускає застосунок знову.

    Окремим процесом PowerShell, бо сам себе процес перезапустити не може:
    мʼютекс єдиного екземпляра й порт 8000 звільняються лише після виходу.
    Чекаємо не довше 60 с — якщо старий процес завис, новий не піднімаємо
    поверх нього (два екземпляри гірші за жоден).
    """
    quoted_args = " ".join("'" + a.replace("'", "''") + "'" for a in args)
    start = f"Start-Process -FilePath '{exe}'"
    if quoted_args:
        start += f" -ArgumentList {quoted_args}"
    script = (
        f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
        f"if ($p) {{ $p.WaitForExit(60000) | Out-Null; if (-not $p.HasExited) {{ exit 1 }} }}; "
        f"{start}"
    )
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script]


def _spawn_relauncher() -> None:
    frozen = bool(getattr(sys, "frozen", False))
    exe = sys.executable
    # Пакований білд: сам exe, без аргументів (--open-browser тут зайвий —
    # оверлей у браузері сам перезавантажить сторінку). Dev-лаунчер: той самий
    # інтерпретатор із тими самими аргументами.
    args = [] if frozen else list(sys.argv)
    command = relaunch_command(os.getpid(), exe, args)
    spawn_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        command,
        creationflags=spawn_flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _install_restarter(server, tray_holder: dict) -> None:
    """Реєструє в `network_access` функцію «перезапуститись»: пауза, щоб
    відповідь роута встигла піти, далі сторож-перезапускач і зупинка сервера
    з треєм — рівно те, що робить «Вийти»."""
    from app.services.network_access import register_restarter

    def _restart_later() -> None:
        time.sleep(1.5)
        try:
            _spawn_relauncher()
        except Exception:
            logging.exception("Не вдалося запустити перезапускач — зупинки не буде")
            return
        logging.info("Перезапуск застосунку на запит із налаштувань")
        server.should_exit = True
        icon = tray_holder.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logging.exception("Tray icon stop failed on restart")

    def _restart() -> None:
        threading.Thread(target=_restart_later, name="kuubmill-restart", daemon=True).start()

    register_restarter(_restart)


def _open_browser_when_ready() -> None:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"{APP_URL}/health", timeout=1):
                webbrowser.open(APP_URL)
                return
        except OSError:
            time.sleep(0.2)


def _backup_database(db_file: Path) -> Path | None:
    # Тонка обгортка навколо app.schema: тека копій читається з модульного
    # DATA_DIR у МОМЕНТ виклику, тому підміна `launcher.DATA_DIR` у тестах
    # і далі влучає (перенос реалізації без цього мовчки зробив би її no-op —
    # та сама пастка, що описана в CLAUDE.md §15 про monkeypatch після переносу).
    from app.schema import backup_database

    return backup_database(db_file, DATA_DIR / "backups")


def _alembic_config():
    from app.schema import alembic_config

    return alembic_config()


def _run_migrations() -> None:
    """Довести базу до голови міграцій ДО імпорту застосунку.

    Класифікація legacy-схем лишається тут (гард знає базову схему 0001), а
    сама послідовність «бекап → штамп/upgrade» живе в app.schema — тим самим
    кодом користується `app.web.lifespan` для решти шляхів запуску.
    """
    from alembic import command

    from app.schema import ensure_schema
    from scripts.migration_guard import main as migration_guard

    db_file = Path(DB_PATH).expanduser().resolve()
    os.environ["DB_PATH"] = str(db_file)
    guard_status = migration_guard()
    if guard_status == 4:
        raise RuntimeError(
            "Схема локальної бази несумісна з цією версією. "
            "Запуск зупинено, дані не змінені."
        )

    if guard_status == 3:
        backup = _backup_database(db_file)
        logging.info("Legacy database backup created: %s", backup)
        command.stamp(_alembic_config(), "0001_initial")

    ensure_schema(db_file, DATA_DIR / "backups")


def _load_tray_image():
    """The bundled brand .ico as a PIL image for the tray, or None if anything
    is missing — a tray is a nicety and must never keep the app from starting."""
    try:
        from PIL import Image

        return Image.open(resource_path("assets/kuubmill.ico"))
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
    if os.name == "nt" and not os.environ.get("KUUBMILL_NONINTERACTIVE"):
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
                    # Один клік у треї гасив усе БЕЗ підтвердження — а з
                    # «Роботою з інших ПК» це вимикає ще й колегу за іншим
                    # ПК (ROADMAP #26). Питаємо один раз, називаємо наслідок.
                    if not _confirm_quit():
                        return
                    server.should_exit = True
                    if icon is not None:
                        icon.stop()

                menu = pystray.Menu(
                    pystray.MenuItem("Відкрити KuubMill", _open, default=True),
                    pystray.MenuItem(
                        "Синхронізація таблиці на паузі",
                        _toggle_pause,
                        checked=_is_paused,
                    ),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Вийти", _quit),
                )
                icon = pystray.Icon("KuubMill", image, "KuubMill", menu)
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
        os.environ["KUUBMILL_SCHEMA_MANAGED"] = "1"
        from app.web import app
        import uvicorn
        from app.services.network_access import set_bound_host

        # 127.0.0.1 або 0.0.0.0 — за перемикачем «Робота з інших ПК». Адреса
        # береться РАЗ: перемикач у налаштуваннях перезапускає застосунок.
        host = _startup_host()
        set_bound_host(host)
        logging.info("KuubMill слухає %s:8000", host)
        config = uvicorn.Config(
            app,
            host=host,
            port=8000,
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=10,
            # Без ProxyHeadersMiddleware: інакше X-Forwarded-For переписує
            # scope["client"], і is_loopback_request означає «що клієнт написав
            # у заголовку», а не «з цього компʼютера» (ревʼю 07.09.26).
            proxy_headers=False,
        )
        server = uvicorn.Server(config)
        _create_shutdown_event()

        # The tray icon (if available) needs the main thread for its Windows
        # message loop, so it holds a reference to the icon to stop on shutdown.
        tray_holder: dict = {}
        _install_restarter(server, tray_holder)

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
            logging.exception("KuubMill failed to start")
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
        if not os.environ.get("KUUBMILL_NONINTERACTIVE"):
            _show_startup_error()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
