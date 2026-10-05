"""Вікно-сповіщення KuubMill поверх усіх програм (tkinter, ОКРЕМИЙ ПРОЦЕС).

Логіка «що й коли показувати» — `app/services/desktop_popup.py`; тут лише
малювання. Вікно живе в окремому процесі (`KuubMill.exe --popup-ui <порт>
<ключ>`, `run_helper`): у процесі сервера потік Tk ділив інтерпретатор з
опитуванням верстатів і печей і отримував кадр раз на секунди — на проді
вікно рухалось ривками ~0.5 кадра/с (власник 05.10.26; замір: 6 зайнятих
потоків уже дають 250 мс між кадрами замість 100). Сервер шле команди
рядками JSON через 127.0.0.1 (`desktop_popup.PopupClient`), процес вікна
відповідає подіями (збережене положення). Закрив сервер з'єднання — процес
вікна завершується сам.

Поведінка (власник 05.10.26, макет `design/kuubmill_native-popup-stack_*`):
  * вікно не забирає фокус (WS_EX_NOACTIVATE): друк у Sum3D не переривається;
  * подія того самого типу НЕ додає вікно — оновлює наявне (число, текст),
    коротко блимає й починає таймер спочатку; інший тип — друге вікно поруч,
    більше двох не буває;
  * наведення мишею зупиняє таймер; клік відкриває KuubMill на потрібній
    сторінці; хрестик закриває;
  * режим «Налаштувати положення»: вікно-зразок можна перетягнути мишею,
    «Зберегти тут» пише координати в налаштування.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Розміри в логічних пікселях (множаться на масштаб екрана).
W, H, GAP, MARGIN = 360, 82, 10, 16
CARD, CARD_EDGE, INK, INK2, ACCENT, KEY = (
    "#1b150c", "#5c4a2c", "#f2e8d8", "#bfae95", "#ffd894", "#010203",
)
TICK_MS = 16  # ~60 кадрів/с: процес вікна нікому не заважає


def _monitors() -> list[tuple[int, int, int, int, bool]]:
    """Робочі області моніторів (без панелі задач): (x0, y0, x1, y1, основний).
    Основний — першим. Не Windows або збій — порожньо (тоді — екран Tk)."""
    if sys.platform != "win32":
        return []
    try:
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        found: list[tuple[int, int, int, int, bool]] = []
        proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
        )

        def _cb(hmon, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcWork
                found.append((r.left, r.top, r.right, r.bottom, bool(info.dwFlags & 1)))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_type(_cb), 0)
        found.sort(key=lambda m: not m[4])
        return found
    except Exception:  # noqa: BLE001 — без переліку моніторів працюємо з екраном Tk
        logger.debug("EnumDisplayMonitors не вдався", exc_info=True)
        return []


def monitor_count() -> int:
    return max(1, len(_monitors()))


def _no_activate(hwnd: int) -> None:
    """Вікно не забирає фокус і не має кнопки на панелі задач."""
    if sys.platform != "win32" or not hwnd:
        return
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = -20, 0x80, 0x08000000, 0x8
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)


def _show_no_activate(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    if sys.platform != "win32" or not hwnd:
        return
    HWND_TOPMOST, SWP_NOACTIVATE, SWP_SHOWWINDOW = -1, 0x10, 0x40
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)


def _fit(text: str, font: tuple, maxw: int) -> str:
    """Обрізати текст до ширини `maxw` пікселів з «…» (Tk має бути запущений)."""
    import tkinter.font as tkfont

    f = tkfont.Font(font=font)
    if f.measure(text) <= maxw:
        return text
    while text and f.measure(text + "…") > maxw:
        text = text[:-1]
    return text.rstrip(" ·") + "…"


class _Card:
    """Одне вікно-сповіщення (Toplevel)."""

    def __init__(self, ui: "PopupUI", kind: str) -> None:
        import tkinter as tk

        self.ui, self.kind = ui, kind
        self.top = tk.Toplevel(ui.root)
        self.top.withdraw()
        self.top.attributes("-alpha", 0.0)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-transparentcolor", KEY)
        except Exception:  # noqa: BLE001 — без прозорості кути просто квадратні
            pass
        s = ui.scale
        self.w, self.h = int(W * s), int(H * s)
        self.cv = tk.Canvas(self.top, width=self.w, height=self.h, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        self.remaining_ms = 0
        self.total_ms = 0
        self.hover = False
        self.path = "/"
        self.alpha = 0.0
        self.closing = False
        self.mapped = False
        self.place_mode = False
        self._drag: tuple[int, int] | None = None
        self._photo: Any = None
        self.cv.bind("<Enter>", lambda _e: setattr(self, "hover", True))
        self.cv.bind("<Leave>", lambda _e: setattr(self, "hover", False))
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.top.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id()) if sys.platform == "win32" else 0
        _no_activate(self.hwnd)

    # ── малювання ──
    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def draw(self, count: int, title: str, body: str, flash: bool = False) -> None:
        s, cv = self.ui.scale, self.cv
        cv.delete("all")
        edge = ACCENT if flash else CARD_EDGE
        self._round_rect(1, 1, self.w - 2, self.h - 2, int(16 * s), fill=CARD, outline=edge, width=max(1, int(1.5 * s)))
        icon = int(48 * s)
        self._photo = self.ui.icon(self.kind, count, icon)
        pad = int(16 * s)
        iy = (self.h - icon) // 2 - int(2 * s)
        if self._photo is not None:
            cv.create_image(pad, iy, image=self._photo, anchor="nw")
        tx = pad + icon + int(14 * s)
        maxw = self.w - tx - int(30 * s)
        # Один рядок на заголовок і один на опис, з «…»: перенесений текст
        # налазив на смужку часу.
        title_font, body_font = ("Segoe UI Semibold", 11), ("Segoe UI", 10)
        cv.create_text(tx, int(24 * s), text=_fit(title, title_font, maxw), anchor="w",
                       fill=INK, font=title_font)
        cv.create_text(tx, int(48 * s), text=_fit(body, body_font, maxw), anchor="w",
                       fill=INK2, font=body_font)
        if not self.place_mode:
            cv.create_text(self.w - int(16 * s), int(16 * s), text="✕", fill=INK2, tags=("close",),
                           font=("Segoe UI", 9))
            cv.tag_bind("close", "<ButtonRelease-1>", lambda _e: self.close())
        self.bar = cv.create_line(int(18 * s), self.h - int(7 * s), int(18 * s), self.h - int(7 * s),
                                  fill=ACCENT, width=max(2, int(2 * s)), capstyle="round")
        self._progress()

    def _progress(self) -> None:
        s = self.ui.scale
        x0, x1 = int(18 * s), self.w - int(18 * s)
        frac = (self.remaining_ms / self.total_ms) if self.total_ms else 1.0
        y = self.h - int(7 * s)
        self.cv.coords(self.bar, x0, y, x0 + (x1 - x0) * max(0.0, min(1.0, frac)), y)

    # ── поведінка ──
    def arm(self, seconds: int, path: str) -> None:
        self.path = path
        self.total_ms = seconds * 1000 if seconds else 0
        self.remaining_ms = self.total_ms

    def place_at(self, x: int, y: int) -> None:
        self.x, self.y = x, y
        self.top.geometry(f"{self.w}x{self.h}+{x}+{y}")
        if not self.mapped:
            # Показ через Tk, щоб він сам відмалював вміст (вікно, показане
            # лише WinAPI, Tk вважав би схованим і міг не малювати). Фокус не
            # забирається: стиль WS_EX_NOACTIVATE стоїть ще до показу, а
            # Windows не віддає передній план фоновому процесу.
            self.top.deiconify()
            self.mapped = True
        if self.hwnd:
            _show_no_activate(self.hwnd, x, y, self.w, self.h)

    def tick(self) -> None:
        if self.closing:
            self.alpha -= 0.08
            if self.alpha <= 0:
                self.destroy()
                return
        elif self.alpha < 0.97:
            self.alpha = min(0.97, self.alpha + 0.06)
        try:
            self.top.attributes("-alpha", self.alpha)
        except Exception:  # noqa: BLE001
            pass
        if self.closing or self.place_mode or not self.total_ms:
            return
        if not self.hover:
            self.remaining_ms -= TICK_MS
            self._progress()
            if self.remaining_ms <= 0:
                self.close()

    def close(self) -> None:
        self.closing = True

    def destroy(self) -> None:
        try:
            self.top.destroy()
        finally:
            self.ui.cards.pop(self.kind, None)
            self.ui.relayout()

    def _press(self, e) -> None:
        if self.place_mode:
            self._drag = (e.x_root - self.x, e.y_root - self.y)

    def _motion(self, e) -> None:
        if self.place_mode and self._drag:
            self.place_at(e.x_root - self._drag[0], e.y_root - self._drag[1])

    def _release(self, e) -> None:
        if self.place_mode:
            self._drag = None
            return
        items = self.cv.find_overlapping(e.x, e.y, e.x, e.y)
        if any("close" in self.cv.gettags(i) for i in items):
            return
        self.ui.open_path(self.path)
        self.close()


class PopupUI:
    """Потік із Tk і черга команд. `start()` — ліниво, при першому показі."""

    def __init__(self, icon_dir: Path, get_settings: Callable[[], Any],
                 save_xy: Callable[[int, int], None], get_origin: Callable[[], str]) -> None:
        self.icon_dir = icon_dir
        self.get_settings = get_settings
        self.save_xy = save_xy
        self.get_origin = get_origin
        self.q: queue.Queue = queue.Queue()
        self.cards: dict[str, _Card] = {}
        self.thread: threading.Thread | None = None
        self.root: Any = None
        self.scale = 1.0
        self._icons: dict[str, Any] = {}
        self._placer: Any = None

    # ── з будь-якого потоку ──
    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="kuubmill-popup-ui", daemon=True)
            self.thread.start()

    def show(self, kind: str, count: int, title: str, body: str, path: str) -> None:
        self.start()
        self.q.put(("show", kind, count, title, body, path))

    def test(self, kind: str = "lab") -> None:
        if kind == "mail":
            self.show("mail", 2, "2 нові листи", "Пробне вікно · клікни, щоб відкрити пошту", "/mail")
        else:
            self.show("lab", 3, "Лабораторія — можна брати 3", "Пробне вікно · Нова: 24122 · моно A3 · 4 од.", "/")

    def place(self) -> None:
        self.start()
        self.q.put(("place",))

    def stop(self) -> None:
        self.q.put(("stop",))

    # ── потік Tk ──
    def _run(self) -> None:
        try:
            if sys.platform == "win32":
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)
                except Exception:  # noqa: BLE001 — старіші Windows: масштаб від Tk
                    pass
            import tkinter as tk

            self.root = tk.Tk()
            self.root.withdraw()
            self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
            self.root.after(TICK_MS, self._loop)
            self.root.mainloop()
        except Exception:  # noqa: BLE001 — вікно не має валити застосунок
            logger.exception("Спливаюче вікно KuubMill не запустилось")
        finally:
            self.root = None
            self.cards.clear()

    def _loop(self) -> None:
        try:
            while True:
                cmd = self.q.get_nowait()
                if cmd[0] == "stop":
                    self.root.quit()
                    return
                if cmd[0] == "show":
                    self._show(*cmd[1:])
                elif cmd[0] == "place":
                    self._place_mode()
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Спливаюче вікно: збій команди")
        for card in list(self.cards.values()):
            card.tick()
        self.root.after(TICK_MS, self._loop)

    def _show(self, kind: str, count: int, title: str, body: str, path: str) -> None:
        settings = self.get_settings()
        card = self.cards.get(kind)
        flash = card is not None and not card.closing
        if card is None or card.closing:
            if card is not None:
                card.destroy()
            card = _Card(self, kind)
            self.cards[kind] = card
        card.arm(settings.seconds, path)
        card.draw(count, title, body, flash=flash)
        self.relayout()
        if flash:
            self.root.after(450, lambda c=card, a=(count, title, body): c.draw(*a) if c.kind in self.cards else None)

    def relayout(self) -> None:
        """Перше вікно — в обраному місці, друге — над ним (для нижніх кутів)
        або під ним (для верхніх). Порядок: лабораторія ближче до кута."""
        if self.root is None:
            return
        settings = self.get_settings()
        order = [k for k in ("lab", "mail") if k in self.cards and not self.cards[k].place_mode]
        for i, kind in enumerate(order):
            card = self.cards[kind]
            x, y = self._anchor_xy(settings, card.w, card.h, i)
            card.place_at(x, y)

    def _anchor_xy(self, settings, w: int, h: int, index: int) -> tuple[int, int]:
        mons = _monitors()
        if mons:
            m = mons[min(settings.monitor, len(mons) - 1)]
        else:
            m = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight(), True)
        x0, y0, x1, y1 = m[0], m[1], m[2], m[3]
        margin, gap = int(MARGIN * self.scale), int(GAP * self.scale)
        step = (h + gap) * index
        if settings.anchor == "custom" and settings.xy:
            x, y = settings.xy
            # Друге вікно росте від центру екрана: унизу — вгору, вгорі — вниз.
            mid = (y0 + y1) / 2
            y = y - step if y > mid else y + step
            return x, y
        right = settings.anchor in ("br", "tr")
        bottom = settings.anchor in ("br", "bl")
        x = x1 - margin - w if right else x0 + margin
        y = (y1 - margin - h - step) if bottom else (y0 + margin + step)
        return x, y

    def icon(self, kind: str, count: int, size: int) -> Any:
        """Іконка з числом у кутку — PIL, як canvas у браузері."""
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk

            base = self._icons.get(kind)
            if base is None:
                base = Image.open(self.icon_dir / f"notify-{'crown' if kind == 'lab' else 'mail'}.png").convert("RGBA")
                self._icons[kind] = base
            img = base.resize((size, size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=max(4, size // 5), fill=255)
            out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            out.paste(img, (0, 0), mask)
            if count > 0:
                d = ImageDraw.Draw(out)
                text = "99+" if count > 99 else str(count)
                r = max(9, size // 5)
                cx, cy = size - r - 1, size - r - 1
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 216, 148, 255), outline=(20, 16, 10, 255), width=max(2, size // 24))
                font: Any
                try:
                    font = ImageFont.truetype("seguisb.ttf", int(r * (1.0 if len(text) < 3 else 0.75)))
                except Exception:  # noqa: BLE001
                    font = ImageFont.load_default()
                tw = d.textlength(text, font=font)
                d.text((cx - tw / 2, cy - r * 0.62), text, font=font, fill=(30, 22, 10, 255))
            return ImageTk.PhotoImage(out)
        except Exception:  # noqa: BLE001 — без іконки вікно все одно корисне
            logger.debug("Іконка сповіщення не намалювалась", exc_info=True)
            return None

    def open_path(self, path: str) -> None:
        try:
            webbrowser.open((self.get_origin() or "http://127.0.0.1:8000") + path)
        except Exception:  # noqa: BLE001
            logger.exception("Не вдалося відкрити KuubMill з вікна-сповіщення")

    def _place_mode(self) -> None:
        """Вікно-зразок, яке можна тягнути, і маленька панель «Зберегти тут»."""
        import tkinter as tk

        if self._placer is not None:
            return
        settings = self.get_settings()
        card = _Card(self, "place")
        card.place_mode = True
        card.arm(0, "/")
        card.draw(3, "Перетягни мене мишею", "Куди поставиш — там і з'являтимуться вікна")
        x, y = self._anchor_xy(settings, card.w, card.h, 0)
        card.place_at(x, y)
        card.alpha = 0.97
        card.top.attributes("-alpha", 0.97)  # зразок не проходить через tick()
        bar = tk.Toplevel(self.root)
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        bar.configure(bg=CARD)

        def _finish(save: bool) -> None:
            if save:
                try:
                    self.save_xy(card.x, card.y)
                except Exception:  # noqa: BLE001
                    logger.exception("Не вдалося зберегти положення вікна")
            bar.destroy()
            card.top.destroy()
            self._placer = None
            self.relayout()

        tk.Button(bar, text="Зберегти тут", command=lambda: _finish(True), bg=ACCENT, fg="#1a1208",
                  relief="flat", font=("Segoe UI Semibold", 10), padx=12, pady=4).pack(side="left", padx=6, pady=6)
        tk.Button(bar, text="Скасувати", command=lambda: _finish(False), bg=CARD, fg=INK,
                  relief="flat", font=("Segoe UI", 10), padx=12, pady=4).pack(side="left", padx=(0, 6), pady=6)
        bar.update_idletasks()

        def _follow() -> None:
            if self._placer is None:
                return
            bar.geometry(f"+{card.x}+{card.y + card.h + int(6 * self.scale)}")
            self.root.after(TICK_MS, _follow)

        self._placer = bar
        _follow()


# ── Процес вікна ───────────────────────────────────────────────────────────


def _settings_from(data: dict) -> Any:
    from app.services.desktop_popup import PopupSettings

    xy = data.get("xy")
    return PopupSettings(
        enabled=True,
        events=frozenset(data.get("events") or ()),
        anchor=str(data.get("anchor") or "br"),
        monitor=int(data.get("monitor") or 0),
        xy=(int(xy[0]), int(xy[1])) if xy else None,
        seconds=int(data.get("seconds") or 0),
    )


def run_helper(port: int, token: str) -> int:
    """Точка входу процесу вікна: підключитись до сервера, читати команди,
    малювати. Tk — у головному потоці цього процесу."""
    import json
    import socket

    _configure_helper_logging()
    try:
        conn = socket.create_connection(("127.0.0.1", port), timeout=10)
        conn.settimeout(None)
        conn.sendall((token + "\n").encode())
    except OSError:
        logger.exception("Процес вікна: не вдалося підключитись до KuubMill")
        return 1

    state: dict[str, Any] = {"settings": _settings_from({}), "origin": ""}
    send_lock = threading.Lock()

    def _save_xy(x: int, y: int) -> None:
        with send_lock:
            conn.sendall((json.dumps({"event": "xy", "x": x, "y": y}) + "\n").encode())

    ui = PopupUI(
        _icon_dir(), lambda: state["settings"], _save_xy, lambda: state["origin"],
    )

    def _reader() -> None:
        buf = b""
        stopping = False
        try:
            while not stopping:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if "settings" in msg:
                        state["settings"] = _settings_from(msg["settings"])
                    if msg.get("origin"):
                        state["origin"] = msg["origin"]
                    cmd = msg.get("cmd")
                    if cmd == "show":
                        ui.q.put(("show", msg["kind"], int(msg.get("count") or 0),
                                  msg.get("title", ""), msg.get("body", ""), msg.get("path", "/")))
                    elif cmd == "place":
                        ui.q.put(("place",))
                    elif cmd == "stop":
                        stopping = True  # вийти з ОБОХ циклів, не лише з розбору
                        break
        except OSError:
            pass
        ui.q.put(("stop",))

    threading.Thread(target=_reader, name="popup-reader", daemon=True).start()
    ui._run()  # Tk у головному потоці; повертається після ("stop",)
    try:
        conn.close()
    except OSError:
        pass
    return 0


def _icon_dir() -> Path:
    from app.runtime import resource_path

    return resource_path("app/static/img")


def _configure_helper_logging() -> None:
    """Збірка без консолі: помилки процесу вікна — у власний файл поруч із
    логом застосунку (`logs/popup-ui.log`)."""
    try:
        from logging.handlers import RotatingFileHandler

        from app.runtime import LOG_FORMAT, data_dir

        logs = data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(logs / "popup-ui.log", maxBytes=512_000, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001 — без лога вікно все одно працює
        pass


if __name__ == "__main__":
    # Dev: `python -m app.desktop_popup_ui --popup-ui <порт> <ключ>`.
    if len(sys.argv) >= 4 and sys.argv[1] == "--popup-ui":
        sys.exit(run_helper(int(sys.argv[2]), sys.argv[3]))
