"""Власне спливаюче вікно KuubMill поверх усіх програм (власник 05.10.26).

Навіщо своє вікно, а не сповіщення браузера: положення браузерного
сповіщення задає Windows (правий нижній кут основного монітора), тривалість
теж (~5 с), і жоден сайт цього не змінить. Власник хоче фіксувати місце й час.
Тому вікно показує сама програма KuubMill — той самий процес, що тримає
сервер і значок у треї (`app/desktop_popup_ui.py`, tkinter в окремому потоці).

Тут — усе, що НЕ потребує вікна, тож тестується без GUI:
  * налаштування (база, ключі `desktop_popup_*`): увімкнено, події, монітор,
    кут або своє місце, скільки висить;
  * «присутність» браузера: яку сторінку KuubMill відкрито на ЦЬОМУ ПК і чи
    вікно у фокусі — браузер повідомляє з кожним опитуванням
    `/api/notify-state`;
  * виявлення нових подій (`PopupWatcher`) і правило «коли показувати»
    (`should_show`).

Правило (власник 05.10.26):
  * браузер згорнутий / інша вкладка / закритий — показуємо все увімкнене;
  * видно, але фокус в іншій програмі (другий монітор): на сторінці пошти
    без листів, на сторінці черги без лабораторії — їх і так видно;
  * KuubMill у фокусі — нічого, досить тосту в застосунку.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import threading
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.settings_store import get_setting, set_setting

KINDS = ("lab", "mail")
ANCHORS = ("br", "bl", "tr", "tl", "custom")

KEY_ENABLED = "desktop_popup_enabled"
KEY_EVENTS = "desktop_popup_events"
KEY_ANCHOR = "desktop_popup_anchor"
KEY_MONITOR = "desktop_popup_monitor"
KEY_XY = "desktop_popup_xy"
KEY_SECONDS = "desktop_popup_seconds"

DEFAULT_SECONDS = 8
MIN_SECONDS = 3
MAX_SECONDS = 60
# Браузер опитує раз на 5–60 с (типово 15). Присутність старша за це — браузер
# закрито або вкладка спить: вважаємо, що оператор KuubMill не бачить.
PRESENCE_FRESH_SECONDS = 45.0


@dataclass(frozen=True)
class PopupSettings:
    enabled: bool = False
    events: frozenset[str] = frozenset(KINDS)
    anchor: str = "br"
    monitor: int = 0
    xy: tuple[int, int] | None = None
    # 0 — висить до кліку.
    seconds: int = DEFAULT_SECONDS


def _int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def load_settings(db: Session) -> PopupSettings:
    """Невідоме/порожнє = вимкнено й типові значення: нова база й зіпсований
    ключ шифрування (`get_setting` → None, §14) не мають раптом показувати
    вікна поверх Sum3D."""
    events_raw = get_setting(db, KEY_EVENTS)
    events = (
        frozenset(k for k in events_raw.split(",") if k in KINDS)
        if events_raw is not None else frozenset(KINDS)
    )
    anchor = get_setting(db, KEY_ANCHOR) or "br"
    xy = None
    raw_xy = get_setting(db, KEY_XY) or ""
    if "," in raw_xy:
        x, y = raw_xy.split(",", 1)
        if x.strip().lstrip("-").isdigit() and y.strip().lstrip("-").isdigit():
            xy = (int(x), int(y))
    if anchor == "custom" and xy is None:
        anchor = "br"
    seconds = _int(get_setting(db, KEY_SECONDS), DEFAULT_SECONDS)
    if seconds != 0:
        seconds = max(MIN_SECONDS, min(MAX_SECONDS, seconds))
    return PopupSettings(
        enabled=(get_setting(db, KEY_ENABLED) or "") == "1",
        events=events,
        anchor=anchor if anchor in ANCHORS else "br",
        monitor=max(0, _int(get_setting(db, KEY_MONITOR), 0)),
        xy=xy,
        seconds=seconds,
    )


def save_settings(
    db: Session,
    *,
    enabled: bool,
    events: set[str],
    anchor: str,
    monitor: int,
    seconds: int,
) -> None:
    """Не комітить — коміт за роутом. Своє місце (`xy`) пише лише режим
    «Налаштувати положення» (`save_custom_xy`), не форма."""
    set_setting(db, KEY_ENABLED, "1" if enabled else "")
    set_setting(db, KEY_EVENTS, ",".join(k for k in KINDS if k in events))
    set_setting(db, KEY_ANCHOR, anchor if anchor in ANCHORS else "br")
    set_setting(db, KEY_MONITOR, str(max(0, monitor)))
    if seconds != 0:
        seconds = max(MIN_SECONDS, min(MAX_SECONDS, seconds))
    set_setting(db, KEY_SECONDS, str(seconds))


def save_custom_xy(db: Session, x: int, y: int) -> None:
    set_setting(db, KEY_XY, f"{int(x)},{int(y)}")
    set_setting(db, KEY_ANCHOR, "custom")


# ── Присутність браузера ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Presence:
    page: str = ""          # "mail" | "queue" | інше
    visible: bool = False   # вкладка не прихована (вікно не згорнуте)
    focused: bool = False   # вікно браузера у фокусі
    origin: str = ""        # http://127.0.0.1:8000 — куди вести клік
    at: float = 0.0         # time.monotonic() звіту


_presence_lock = threading.Lock()
_presence = Presence()


def note_presence(page: str, visible: bool, focused: bool, origin: str, now: float | None = None) -> None:
    """Звіт браузера ЦЬОГО ПК (лише з петлі — перевіряє роут). Кілька вкладок:
    перемагає та, що у фокусі або видима, — саме її бачить оператор."""
    global _presence
    t = time.monotonic() if now is None else now
    new = Presence(page=page, visible=visible, focused=focused, origin=origin, at=t)
    with _presence_lock:
        old = _presence
        fresh_old = t - old.at < 5.0
        # Свіжий звіт видимої/сфокусованої вкладки не перебиває фонова вкладка,
        # що відзвітувала слідом.
        if fresh_old and (old.focused or old.visible) and not (focused or visible):
            return
        _presence = new


def current_presence() -> Presence:
    with _presence_lock:
        return _presence


def should_show(kind: str, presence: Presence, now: float | None = None) -> bool:
    """Чи показувати вікно про подію `kind` при такому стані браузера."""
    t = time.monotonic() if now is None else now
    if t - presence.at > PRESENCE_FRESH_SECONDS or not presence.visible:
        return True  # браузер закритий, згорнутий або інша вкладка
    if presence.focused:
        return False  # KuubMill перед очима — досить тосту
    if kind == "mail" and presence.page == "mail":
        return False
    if kind == "lab" and presence.page == "queue":
        return False
    return True


# ── Виявлення подій ───────────────────────────────────────────────────────


@dataclass
class PopupEvent:
    kind: str
    count: int
    title: str
    body: str
    path: str


def _plural(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    if m10 == 1 and m100 != 11:
        return one
    if 2 <= m10 <= 4 and not 12 <= m100 <= 14:
        return few
    return many


@dataclass
class PopupWatcher:
    """Порівнює знімки й віддає ПОДІЇ (приріст), як браузерні тости: перший
    знімок — лише база. Лабораторія — за id (взяли одну, прийшла інша — теж
    подія), пошта — за приростом числа у «Вхідних»."""

    lab_ids: set[int] | None = None
    mail_count: int | None = None
    labels: dict[int, str] = field(default_factory=dict)

    def tick(self, lab_items: list[tuple[int, str]], mail_count: int) -> list[PopupEvent]:
        events: list[PopupEvent] = []
        ids = {oid for oid, _label in lab_items}
        self.labels = dict(lab_items)
        if self.lab_ids is not None:
            arrived = sorted(ids - self.lab_ids)
            if arrived:
                total = len(ids)
                newest = self.labels.get(arrived[-1], "")
                body = ("Нова: " + newest) if newest else "Нова робота з шляхом до папки"
                if len(arrived) > 1:
                    body += f" і ще {len(arrived) - 1}"
                events.append(PopupEvent(
                    "lab", total, f"Лабораторія — можна брати {total}", body, "/",
                ))
        if self.mail_count is not None and mail_count > self.mail_count:
            n = mail_count - self.mail_count
            events.append(PopupEvent(
                "mail", mail_count,
                f"{n} {_plural(n, 'новий лист', 'нові листи', 'нових листів')}",
                "Нові з пошти · клікни, щоб відкрити", "/mail",
            ))
        self.lab_ids = ids
        self.mail_count = mail_count
        return events


def snapshot(db: Session) -> tuple[list[tuple[int, str]], int]:
    """Ті самі числа, що бейджі рейки: сьогоднішня лабораторія «можна брати»
    (`deps.queue_can_take_items_uncached`) і листи у «Вхідних» (`in_inbox`)."""
    from app.mail_inbox import in_inbox
    from app.models import EmailMessage
    from app.routers.deps import queue_can_take_items_uncached

    mail = db.scalar(select(func.count()).select_from(EmailMessage).where(in_inbox)) or 0
    return queue_can_take_items_uncached(), int(mail)


# ── Процес вікна ──────────────────────────────────────────────────────────


def _settings_payload(s: PopupSettings) -> dict:
    return {
        "events": sorted(s.events), "anchor": s.anchor, "monitor": s.monitor,
        "xy": list(s.xy) if s.xy else None, "seconds": s.seconds,
    }


class PopupClient:
    """Сторона сервера: запускає процес вікна (`KuubMill.exe --popup-ui`) і шле
    йому команди рядками JSON через 127.0.0.1.

    Окремий процес, а не потік: потік Tk у процесі сервера ділив інтерпретатор
    з опитуванням верстатів і печей і на проді малював ~0.5 кадра/с (власник
    05.10.26). Порт — випадковий, приймається одне з'єднання з одноразовим
    ключем: інший процес на ПК чужого вікна не намалює. Процес вікна живе,
    поки відкрите з'єднання; помер — наступна команда запустить новий.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Any = None
        self._proc: Any = None

    def _alive(self) -> bool:
        return self._conn is not None and self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> None:
        import secrets
        import socket
        import subprocess
        import sys
        from pathlib import Path

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)
        port = listener.getsockname()[1]
        token = secrets.token_hex(16)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--popup-ui", str(port), token]
            cwd = None
        else:
            cmd = [sys.executable, "-m", "app.desktop_popup_ui", "--popup-ui", str(port), token]
            cwd = str(Path(__file__).resolve().parents[2])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, cwd=cwd, creationflags=flags, close_fds=True)
        try:
            conn, _addr = listener.accept()
        finally:
            listener.close()
        conn.settimeout(10)
        got = b""
        while not got.endswith(b"\n"):
            chunk = conn.recv(256)
            if not chunk:
                break
            got += chunk
        if got.strip().decode(errors="replace") != token:
            conn.close()
            proc.kill()
            raise RuntimeError("процес вікна не підтвердив ключ")
        conn.settimeout(None)
        self._conn, self._proc = conn, proc
        threading.Thread(target=self._read_events, args=(conn,), name="kuubmill-popup-events", daemon=True).start()

    def _read_events(self, conn) -> None:
        """Події від процесу вікна: збережене положення («Зберегти тут»)."""
        import json

        from app.db import SessionLocal

        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if msg.get("event") == "xy":
                        with SessionLocal() as db:
                            save_custom_xy(db, int(msg["x"]), int(msg["y"]))
                            db.commit()
        except Exception:  # noqa: BLE001 — обрив з'єднання = процес вікна завершився
            pass

    def _send(self, msg: dict) -> None:
        import json
        import logging

        from app.db import SessionLocal

        with SessionLocal() as db:
            msg["settings"] = _settings_payload(load_settings(db))
        msg["origin"] = current_presence().origin
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            for attempt in (1, 2):
                try:
                    if not self._alive():
                        self._spawn()
                    self._conn.sendall(data)
                    return
                except Exception:  # noqa: BLE001 — перезапустити процес і спробувати ще раз
                    self._close()
                    if attempt == 2:
                        logging.getLogger(__name__).exception("Спливаюче вікно KuubMill не запустилось")

    def _close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except OSError:
            pass
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._conn = self._proc = None

    def show(self, kind: str, count: int, title: str, body: str, path: str) -> None:
        self._send({"cmd": "show", "kind": kind, "count": count, "title": title, "body": body, "path": path})

    def test(self, kind: str = "lab") -> None:
        if kind == "mail":
            self.show("mail", 2, "2 нові листи", "Пробне вікно · клікни, щоб відкрити пошту", "/mail")
        else:
            self.show("lab", 3, "Лабораторія — можна брати 3", "Пробне вікно · Нова: 24122 · моно A3 · 4 од.", "/")

    def place(self) -> None:
        self._send({"cmd": "place"})

    def stop(self) -> None:
        with self._lock:
            if self._alive():
                try:
                    self._conn.sendall(b'{"cmd": "stop"}\n')
                    self._proc.wait(timeout=2)  # хай закриє вікна сам
                except Exception:  # noqa: BLE001 — не вийшов сам — _close() завершить
                    pass
            self._close()


_client: PopupClient | None = None
_client_lock = threading.Lock()


def supported() -> bool:
    """Вікно малює програма KuubMill на ПК цеху (Windows). Під Linux/CI — ні."""
    import sys

    return sys.platform == "win32"


def get_popup_ui() -> PopupClient:
    """Один клієнт процесу вікна на сервер. Процес запускається при першому
    показі, а не на старті застосунку."""
    global _client
    with _client_lock:
        if _client is None:
            _client = PopupClient()
        return _client
