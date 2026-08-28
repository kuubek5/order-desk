"""Автоматизація Провідника Windows — винесено з web.py (Крок 2 розбиття).

Тут єдине місце, прибите до конкретної ОС: відкрити теку в Провіднику й
підняти її вікно наперед (user32/ctypes). HTTP-шар не має цього знати — він
лише кличе `open_folder_in_explorer`. Якщо колись знадобиться Linux/Mac,
чіпати доведеться лише цей файл.

Публічне API: `open_folder_in_explorer(folder)`. Решта — деталі підняття
вікна (пошук нового вікна різницею, наполягання проти згортання назад тощо),
докладно описані в бойових коментарях нижче.
"""

import logging
import os
import subprocess
import time
from pathlib import Path
from threading import Thread

logger = logging.getLogger("app.platform_windows")


def _open_folder_in_explorer(folder: Path) -> None:
    if os.name != "nt":
        raise NotImplementedError
    # Launch a fresh explorer.exe rather than os.startfile: ShellExecute (which
    # startfile uses) hands an ALREADY-OPEN Explorer the path and that window
    # stays wherever it was — often behind the browser, which is exactly the
    # "opens in the background" complaint. explorer.exe <path> opens a new
    # window that comes to the foreground. AllowSetForegroundWindow lifts the
    # foreground lock so the shell may raise it even though this call comes from
    # the background server process. explorer.exe returns exit code 1 on success
    # (a known quirk), so the return code is deliberately ignored.
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:  # noqa: BLE001 — foreground hint is best-effort
        pass
    # Ask for a NORMAL (restored, visible) window rather than whatever state the
    # shell last used — operators reported the folder opening minimized. Passed
    # via STARTUPINFO.wShowWindow (SW_SHOWNORMAL); a hint the shell honours for a
    # fresh window and harmlessly ignores otherwise.
    startupinfo = None
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
    except Exception:  # noqa: BLE001 — Windows-only struct; never block the open
        startupinfo = None
    # Знімок вікон Провідника ДО запуску: нове вікно потім знаходиться
    # різницею, без здогадів про заголовок.
    before = set(_explorer_windows())
    subprocess.Popen(["explorer", str(folder)], startupinfo=startupinfo)  # noqa: S603,S607
    # Підказки вище — необхідні, але недостатні: `explorer.exe <шлях>` лише
    # передає шлях УЖЕ ЗАПУЩЕНІЙ оболонці, а вікно створює вона, у своєму
    # процесі. STARTUPINFO запущеного нами стабу на те вікно не діє, тож воно
    # й далі з'являлось згорнутим або за браузером. Тому вікно піднімається
    # окремо, коли з'явиться. У фоновому потоці: чекати на нього всередині
    # запиту означало б тримати оператора на «крутилці» заради вікна, яке він
    # і так уже бачить.
    Thread(
        target=_raise_explorer_window,
        args=(folder, before),
        name="order-desk-raise-explorer",
        daemon=True,
    ).start()


_EXPLORER_WINDOW_CLASSES = ("CabinetWClass", "ExploreWClass")
_RAISE_WINDOW_TIMEOUT_SECONDS = 3.0


def _explorer_windows() -> list[int]:
    """Дескриптори всіх відкритих вікон Провідника, зверху вниз за z-порядком."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def _callback(hwnd, _lparam):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        # Тільки видимі: Провідник тримає й службові вікна свого класу, і
        # схопити таке замість справжнього означало б «підняти» ніщо.
        if buffer.value in _EXPLORER_WINDOW_CLASSES and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(_callback), 0)
    return found


def _window_title(hwnd) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def _titles_this_folder(title: str, target: str) -> bool:
    """Чи це вікно показує саме нашу теку.

    Заголовок вікна — не назва теки: Провідник дописує локалізований суфікс
    («Pavlenko — проводник»), і в різних складаннях Windows розділювач інший.
    Тому не рівність і не розбір за тире, а префікс із перевіркою, що далі йде
    не буква — інакше тека `A` збігалась би з `Abc`."""
    lowered = title.strip().lower()
    if lowered == target:
        return True
    if not lowered.startswith(target):
        return False
    tail = lowered[len(target):1 + len(target)]
    return not tail.isalnum()


_INSIST_SHOWN_SECONDS = 3.0
_INSIST_CALM_TICKS = 4


def _insist_window_shown(user32, hwnd) -> bool:
    """Розгортати вікно, доки воно не лишиться розгорнутим.

    Одного `SW_RESTORE` не досить, і це не здогад: у бойовому логу 28.08.26
    стоїть «Провідник піднято (нове вікно)», а оператор бачив згорнуте вікно.
    Провідник застосовує збережене положення ВЖЕ ПІСЛЯ створення вікна, тож
    ми виграємо гонку й одразу програємо її — він згортає вікно назад.

    Тому наполягаємо: поки вікно згорнуте — розгортаємо знову, і виходимо
    лише коли воно кілька перевірок поспіль лишилось розгорнутим. Повертаємо
    підсумковий стан, щоб у лог ішов ФАКТ, а не намір."""
    deadline = time.monotonic() + _INSIST_SHOWN_SECONDS
    calm = 0
    while time.monotonic() < deadline:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SwitchToThisWindow(hwnd, True)
            user32.SetForegroundWindow(hwnd)
            calm = 0
        else:
            calm += 1
            if calm >= _INSIST_CALM_TICKS:
                break
        time.sleep(0.15)
    return not bool(user32.IsIconic(hwnd))


def _raise_explorer_window(
    folder: Path,
    before: set | None = None,
    timeout: float = _RAISE_WINDOW_TIMEOUT_SECONDS,
) -> None:
    """Розгорнути й винести наперед вікно Провідника для цієї теки.

    Вікно шукається двома шляхами, бо кожен окремо має сліпу зону:

    * **нове вікно** — різниця зі знімком, зробленим ДО запуску. Не залежить
      ні від заголовка, ні від локалізації Windows;
    * **за заголовком** — на випадок, коли Провідник не створив вікна, а віддав
      уже відкрите (саме тоді воно й лишається згорнутим).

    Перша версія покладалась ЛИШЕ на заголовок і на проді не спрацювала
    (0.3.27, скарга 28.08.26) — звідси і другий шлях, і запис у лог, що саме
    спрацювало: інакше причину знову довелось би вгадувати.

    `ShowWindow(SW_RESTORE)` розгортає згорнуте й не потребує прав на передній
    план; `SwitchToThisWindow` виносить наперед, не спотикаючись об блокування
    переднього плану (сервер — фоновий процес, `SetForegroundWindow` там часто
    просто ігнорують)."""
    if os.name != "nt":
        return
    target = (folder.name or str(folder)).strip().lower()
    if not target:
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        known = set(before or ())
        deadline = time.monotonic() + timeout
        while True:
            windows = _explorer_windows()
            fresh = [h for h in windows if h not in known]
            hwnd, how = (fresh[0], "нове вікно") if fresh else (None, "")
            if hwnd is None:
                for candidate in windows:
                    if _titles_this_folder(_window_title(candidate), target):
                        hwnd, how = candidate, "наявне вікно за заголовком"
                        break
            if hwnd is not None:
                shown = _insist_window_shown(user32, hwnd)
                logger.info(
                    "Провідник піднято (%s, згорнуте=%s): %s", how, not shown, folder
                )
                return
            if time.monotonic() >= deadline:
                logger.info("Вікно Провідника не знайдено за %.0fс: %s", timeout, folder)
                return
            time.sleep(0.1)
    except Exception:  # noqa: BLE001 — тека вже відкрита; підняття вікна не критичне
        logger.debug("Не вдалося підняти вікно Провідника", exc_info=True)




# Публічне ім'я для HTTP-шару; внутрішня реалізація лишає бойові коментарі.
open_folder_in_explorer = _open_folder_in_explorer
