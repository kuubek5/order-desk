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


_INSIST_SHOWN_SECONDS = 4.0
_INSIST_CALM_TICKS = 7

_SW_SHOW = 5
_SW_RESTORE = 9
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOSIZE_NOMOVE_SHOW = 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW


def _same_window(a, b) -> bool:
    # `GetForegroundWindow` без restype повертає знаковий c_int, а дескриптор
    # з EnumWindows — беззнаковий; значущі в HWND лише нижні 32 біти.
    return bool(a) and bool(b) and (int(a) & 0xFFFFFFFF) == (int(b) & 0xFFFFFFFF)


def _bring_to_front(user32, kernel32, hwnd) -> bool:
    """Розгорнути вікно й винести його НАД браузером. Повертає, чи воно
    тепер на передньому плані.

    Сервер — фоновий процес, і Windows не дає йому забрати передній план у
    браузера, де оператор щойно клацнув: `SetForegroundWindow` мовчки не
    спрацьовує, вікно лишається ЗА розгорнутим браузером, а на панелі задач
    блимає кнопка. Для оператора це те саме «відкрилось згорнутим» (скарга
    11.09.26) — і попередня версія в цьому випадку не робила нічого, бо
    реагувала лише на справді згорнуте вікно (`IsIconic`).

    Тому два кроки:
    1. `AttachThreadInput` до потоку вікна, яке зараз на передньому плані:
       на час виклику ми ділимо з ним стан вводу, і `SetForegroundWindow`
       дозволено. Без імітації натискань — браузер не отримує жодної клавіші.
    2. Якщо все одно не вийшло — хоча б z-порядок: TOPMOST і одразу
       NOTOPMOST ставить вікно поверх усіх звичайних, не лишаючи його
       «завжди зверху». Вікно видно, навіть коли фокус лишився в браузері."""
    user32.ShowWindow(hwnd, _SW_RESTORE if user32.IsIconic(hwnd) else _SW_SHOW)
    foreground = user32.GetForegroundWindow()
    if _same_window(foreground, hwnd):
        return True
    ours = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = bool(theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)
    if _same_window(user32.GetForegroundWindow(), hwnd):
        return True
    user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOSIZE_NOMOVE_SHOW)
    user32.SetWindowPos(hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOSIZE_NOMOVE_SHOW)
    user32.SwitchToThisWindow(hwnd, True)
    return _same_window(user32.GetForegroundWindow(), hwnd)


def _insist_window_shown(user32, kernel32, hwnd) -> tuple[bool, bool]:
    """Підняти вікно й стежити, щоб воно не згорнулось назад.

    Одного `SW_RESTORE` не досить, і це не здогад: у бойовому логу 28.08.26
    стоїть «Провідник піднято (нове вікно)», а оператор бачив згорнуте вікно.
    Провідник застосовує збережене положення ВЖЕ ПІСЛЯ створення вікна, тож
    ми виграємо гонку й одразу програємо її — він згортає вікно назад.

    Тому наперед виносимо один раз (далі фокус не відбираємо: оператор міг
    уже клацнути деінде), а згорнуте розгортаємо знову щоразу, і виходимо
    лише коли воно ~1 с поспіль лишилось розгорнутим. Повертаємо підсумковий
    стан (розгорнуте, на передньому плані), щоб у лог ішов ФАКТ, а не намір."""
    front = _bring_to_front(user32, kernel32, hwnd)
    deadline = time.monotonic() + _INSIST_SHOWN_SECONDS
    calm = 0
    while time.monotonic() < deadline:
        time.sleep(0.15)
        if user32.IsIconic(hwnd):
            front = _bring_to_front(user32, kernel32, hwnd)
            calm = 0
        else:
            calm += 1
            if calm >= _INSIST_CALM_TICKS:
                break
    return not bool(user32.IsIconic(hwnd)), front


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
        kernel32 = ctypes.windll.kernel32
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
                shown, front = _insist_window_shown(user32, kernel32, hwnd)
                logger.info(
                    "Провідник піднято (%s, згорнуте=%s, наперед=%s): %s",
                    how, not shown, front, folder,
                )
                return
            if time.monotonic() >= deadline:
                logger.info("Вікно Провідника не знайдено за %.0fс: %s", timeout, folder)
                return
            time.sleep(0.1)
    except Exception:  # noqa: BLE001 — тека вже відкрита; підняття вікна не критичне
        logger.debug("Не вдалося підняти вікно Провідника", exc_info=True)




# ── RustDesk (власник 30.09.26) ───────────────────────────────────────────
# Та сама біда, що з теками: посилання `rustdesk://` з браузера відкриває
# з'єднання, але вікно RustDesk лишається ЗА браузером — Windows не дає йому
# самому забрати передній план. Тому на ПК, де стоїть CRM, RustDesk запускає
# сервер і піднімає його вікно тими самими `_bring_to_front`/
# `_insist_window_shown`, що вже довели себе на Провіднику.

_RUSTDESK_IMAGE = "rustdesk.exe"
# З'єднання встановлюється секунди: вікно сесії з'являється не одразу.
_RUSTDESK_WAIT_SECONDS = 8.0


def _process_image(hwnd) -> str:
    """Імʼя exe-файлу процесу, якому належить вікно (нижній регістр)."""
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def _rustdesk_windows() -> list[tuple[int, str]]:
    """Видимі вікна RustDesk (дескриптор, заголовок), зверху вниз."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[tuple[int, str]] = []

    def _callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = _window_title(hwnd)
            if title and _process_image(hwnd) == _RUSTDESK_IMAGE:
                found.append((hwnd, title))
        return True

    user32.EnumWindows(enum_proc(_callback), 0)
    return found


def pick_rustdesk_window(
    windows: list[tuple[int, str]], before: set, remote_id: str, name: str
) -> tuple[int | None, str]:
    """Котре вікно RustDesk підняти. Окремо від WinAPI — щоб перевірялось тестом.

    1. нове вікно (різниця зі знімком до запуску) — окреме вікно сесії;
    2. заголовок називає ID чи назву верстата — сесія відкрилась вкладкою в
       уже наявному вікні, і воно тепер показує її;
    3. заголовок містить ЗНАЧУЩЕ слово з назви верстата («Olejka» з
       «150i-Olejka»): RustDesk підписує вікно сесії `користувач@ПК@серійник -
       Remote Desktop`, а не нашою назвою (лог цеху 01.10.26 —
       `150@Olejka@sn2023s1297`, `350@Loaderr@350i`);
    4. вікно сесії рівно ОДНЕ — нове з'єднання відкрилось вкладкою саме в
       ньому, хоч заголовок ще показує попередню вкладку;
    5. інакше — нічого: краще не підняти, ніж підняти чужу сесію."""
    fresh = [(h, t) for h, t in windows if h not in before]
    if fresh:
        return fresh[0][0], "нове вікно"
    rid = "".join((remote_id or "").split()).casefold()
    label = (name or "").strip().casefold()
    for hwnd, title in windows:
        flat = "".join(title.split()).casefold()
        if (rid and rid in flat) or (label and label in title.casefold()):
            return hwnd, "за заголовком"
    sessions = [(h, t) for h, t in windows if _RUSTDESK_SESSION_MARK in t.casefold()]
    words = _name_words(label)
    if words:
        hits = [h for h, t in sessions if any(w in t.casefold() for w in words)]
        if len(hits) == 1:
            return hits[0], "за словом назви"
    if len(sessions) == 1:
        return sessions[0][0], "єдине вікно сесії"
    return None, ""


# Так RustDesk підписує вікно віддаленої сесії (на відміну від головного
# вікна «RustDesk»).
_RUSTDESK_SESSION_MARK = "remote desktop"


def _name_words(label: str) -> list[str]:
    """Значущі слова назви верстата: від 4 літер і не самі цифри з моделлю
    («150i», «350»). Коротке («Sec», «New») збігалося б із чим завгодно."""
    import re

    words = re.split(r"[^0-9a-zа-яіїєґ]+", label.casefold())
    return [w for w in words if len(w) >= 4 and not re.fullmatch(r"\d+[a-z]?", w)]


def _raise_rustdesk_window(remote_id: str, name: str, before: set,
                           timeout: float = _RUSTDESK_WAIT_SECONDS) -> None:
    try:
        import ctypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        deadline = time.monotonic() + timeout
        while True:
            windows = _rustdesk_windows()
            hwnd, how = pick_rustdesk_window(windows, before, remote_id, name)
            if hwnd is not None:
                shown, front = _insist_window_shown(user32, kernel32, hwnd)
                logger.info(
                    "RustDesk піднято (%s, згорнуте=%s, наперед=%s): %s «%s»",
                    how, not shown, front, remote_id, _window_title(hwnd),
                )
                return
            if time.monotonic() >= deadline:
                # Заголовки в лог — щоб правило вибору вікна правилось за
                # фактами з цеху, а не вгадувалось (CLAUDE.md §15).
                logger.info(
                    "Вікно RustDesk для %s (%s) не знайдено за %.0fс; вікна RustDesk: %s",
                    remote_id, name, timeout, [t for _, t in windows],
                )
                return
            time.sleep(0.2)
    except Exception:  # noqa: BLE001 — з'єднання вже відкрито; підняття не критичне
        logger.debug("Не вдалося підняти вікно RustDesk", exc_info=True)


def _open_rustdesk(url: str, remote_id: str, name: str) -> None:
    """Відкрити з'єднання RustDesk на ЦЬОМУ ПК і винести його вікно наперед."""
    if os.name != "nt":
        raise NotImplementedError
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:  # noqa: BLE001 — лише підказка
        pass
    before = {h for h, _ in _rustdesk_windows()}
    os.startfile(url)  # noqa: S606 — протокол RustDesk, як із браузера
    Thread(
        target=_raise_rustdesk_window,
        args=(remote_id, name, before),
        name="kuubmill-raise-rustdesk",
        daemon=True,
    ).start()


# Публічне ім'я для HTTP-шару; внутрішня реалізація лишає бойові коментарі.
open_folder_in_explorer = _open_folder_in_explorer
open_rustdesk = _open_rustdesk
