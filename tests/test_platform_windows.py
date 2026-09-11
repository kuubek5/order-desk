"""«Відкрити папку»: вікно Провідника мусить опинитись НАД браузером (11.09.26).

Скарга: тека відкривається «згорнутою». Попередня версія реагувала лише на
справді згорнуте вікно (`IsIconic`); вікно, яке Windows поставила ЗА браузер
(фоновий процес не має права на передній план), лишалось там — і на панелі
задач лише блимала кнопка. Тут фейковий user32 відтворює обидва випадки.
"""

from __future__ import annotations

from app import platform_windows as pw

HWND = 0x1A2B3C
BROWSER = 0x0F0F0F


class FakeUser32:
    def __init__(self, *, iconic: bool = False, allow_foreground: bool = True,
                 allow_only_attached: bool = False, reminimize_times: int = 0):
        self.iconic = iconic
        self.foreground = BROWSER
        self.allow_foreground = allow_foreground
        self.allow_only_attached = allow_only_attached
        self.attached = False
        self.reminimize_times = reminimize_times
        self.calls: list[tuple] = []

    def IsIconic(self, hwnd):
        if not self.iconic and self.reminimize_times and self.calls:
            # Провідник згортає вікно назад уже після нашого розгортання.
            self.reminimize_times -= 1
            self.iconic = True
        return self.iconic

    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", cmd))
        if cmd == pw._SW_RESTORE:
            self.iconic = False

    def GetForegroundWindow(self):
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd, _pid):
        return 777 if hwnd == BROWSER else 555

    def AttachThreadInput(self, ours, theirs, attach):
        self.calls.append(("AttachThreadInput", theirs, attach))
        self.attached = bool(attach)
        return True

    def BringWindowToTop(self, hwnd):
        self.calls.append(("BringWindowToTop",))

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", self.attached))
        if self.allow_foreground and (self.attached or not self.allow_only_attached):
            self.foreground = hwnd
        return self.foreground == hwnd

    def SetWindowPos(self, hwnd, after, *rest):
        self.calls.append(("SetWindowPos", after))

    def SwitchToThisWindow(self, hwnd, alt_tab):
        self.calls.append(("SwitchToThisWindow",))


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 111


def test_window_behind_the_browser_is_brought_forward_not_ignored(monkeypatch):
    """Незгорнуте вікно за браузером — саме той випадок, де старий код мовчав."""
    monkeypatch.setattr(pw.time, "sleep", lambda _s: None)
    user32 = FakeUser32(iconic=False, allow_only_attached=True)
    shown, front = pw._insist_window_shown(user32, FakeKernel32(), HWND)
    assert shown and front
    assert ("SetForegroundWindow", True) in user32.calls, "передній план — через AttachThreadInput"
    assert ("AttachThreadInput", 777, False) in user32.calls and not user32.attached, "потік відчеплено"


def test_denied_foreground_still_puts_window_above_the_browser(monkeypatch):
    monkeypatch.setattr(pw.time, "sleep", lambda _s: None)
    """Windows відмовила в передньому плані — вікно все одно над браузером
    (TOPMOST і одразу NOTOPMOST), а не «завжди зверху»."""
    user32 = FakeUser32(iconic=False, allow_foreground=False)
    shown, front = pw._insist_window_shown(user32, FakeKernel32(), HWND)
    assert shown and not front
    positions = [c[1] for c in user32.calls if c[0] == "SetWindowPos"]
    assert positions == [pw._HWND_TOPMOST, pw._HWND_NOTOPMOST]
    assert not user32.attached


def test_minimized_window_is_restored_again_when_explorer_minimizes_it_back(monkeypatch):
    monkeypatch.setattr(pw.time, "sleep", lambda _s: None)
    user32 = FakeUser32(iconic=True, reminimize_times=2)
    shown, front = pw._insist_window_shown(user32, FakeKernel32(), HWND)
    assert shown and front
    restores = [c for c in user32.calls if c == ("ShowWindow", pw._SW_RESTORE)]
    assert len(restores) == 3, "розгорнули спершу і ще двічі після згортання назад"


def test_foreground_is_taken_once_not_fought_over(monkeypatch):
    """Оператор клацнув назад у браузер — вікно не відбирає фокус знову."""
    monkeypatch.setattr(pw.time, "sleep", lambda _s: None)
    user32 = FakeUser32(iconic=False)
    original = user32.SetForegroundWindow

    def set_foreground(hwnd):
        result = original(hwnd)
        user32.foreground = BROWSER  # одразу клацнув у браузер
        return result

    user32.SetForegroundWindow = set_foreground
    pw._insist_window_shown(user32, FakeKernel32(), HWND)
    assert sum(1 for c in user32.calls if c[0] == "BringWindowToTop") == 1


def test_handles_compared_by_low_32_bits():
    """GetForegroundWindow без restype — знаковий int; той самий дескриптор."""
    assert pw._same_window(-2, 0xFFFFFFFE)
    assert not pw._same_window(0, 0)
    assert not pw._same_window(HWND, BROWSER)
