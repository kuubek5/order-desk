"""Табло пічок для логістів — окремий вхід у мережі цеху, лише перегляд.

Рішення власника 11.09.26: логісти в тій самій мережі, дивляться з ПК (і з
телефона), потрібні лише температура і коли відкриється. Мокап —
`app/static/mock/furnace_board.html`.

ЧОМУ ОКРЕМИЙ ПОРТ, А НЕ «ВІДКРИТИ KUUBMILL У МЕРЕЖУ». Застосунок слухає лише
`127.0.0.1`. Відкрити його для мережі — це виставити назовні чергу, пошту й
налаштування (хай і за входом); логістам треба ОДНА сторінка, і акаунти їм
заводити зайве. Тому табло — окремий маленький ASGI-застосунок на своєму
порту (`BOARD_PORT`), у якому фізично немає інших маршрутів: лише сторінка
печей за посиланням із секретом, кілька файлів оформлення — і все. Нічого не
змінює, входу не має.

Посилання — `http://<ПК>:8010/t/<секрет>`. Секрет генерується тут, змінюється
кнопкою в налаштуваннях (старе посилання одразу перестає працювати). Вимкнене
табло не слухає порт узагалі.

Та сама чесність, що й у звіті логістам: табло не прочиталось — «немає
даних», а не старе число; внизу — «дані станом на HH:MM».
"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.business_day import business_now
from app.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

BOARD_PORT = 8010
BOARD_ENABLED_KEY = "furnace_board_enabled"
BOARD_TOKEN_KEY = "furnace_board_token"
# Як часто сторінка сама перечитує картки.
BOARD_REFRESH_SECONDS = 30
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


# ── Налаштування ────────────────────────────────────────────────────────────


def board_enabled(db: Session) -> bool:
    return (get_setting(db, BOARD_ENABLED_KEY) or "") == "1"


def board_token(db: Session) -> Optional[str]:
    value = (get_setting(db, BOARD_TOKEN_KEY) or "").strip()
    return value or None


def regenerate_token(db: Session) -> str:
    """Новий секрет посилання; старе посилання перестає працювати. Не комітить."""
    token = secrets.token_urlsafe(12)
    set_setting(db, BOARD_TOKEN_KEY, token)
    return token


def token_matches(db: Session, candidate: str) -> bool:
    token = board_token(db)
    if not token:
        return False
    return secrets.compare_digest(token.encode(), (candidate or "").encode())


def lan_addresses() -> list[str]:
    """IPv4-адреси цього ПК у локальній мережі — для посилання в налаштуваннях.

    Спершу адреса, через яку ПК виходить назовні (UDP-«з'єднання» нікуди не
    шле пакетів, лише питає систему маршрут), потім решта з імені хоста.
    Петля 127.x — ні: з іншого ПК вона не відкриється."""
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address not in found:
                found.append(address)
    except OSError:
        pass
    return [a for a in found if not a.startswith("127.")]


def board_links(db: Session) -> list[str]:
    token = board_token(db)
    if not token:
        return []
    return [f"http://{address}:{BOARD_PORT}/t/{token}" for address in lan_addresses()]


# ── Що показати ─────────────────────────────────────────────────────────────


@dataclass
class BoardCard:
    name: str
    kind: str  # "run" | "idle" | "bad"
    temp: Optional[int] = None
    open_day: str = ""   # "" — сьогодні, «завтра», «пт 12.09»
    open_at: str = ""    # «23:40»
    done_iso: str = ""   # для відліку «ще …» у браузері
    remaining: str = ""
    note: str = ""


@dataclass
class BoardView:
    now: datetime
    cards: list[BoardCard] = field(default_factory=list)
    nearest_name: str = ""
    nearest_at: str = ""
    nearest_day: str = ""
    nearest_iso: str = ""
    as_of: str = ""

    @property
    def date_label(self) -> str:
        return f"{_WEEKDAYS[self.now.weekday()]} {self.now:%d.%m}"


def _open_day(done_at: datetime, now: datetime) -> str:
    if done_at.date() == now.date():
        return ""
    if done_at.date() == (now + timedelta(days=1)).date():
        return "завтра"
    return f"{_WEEKDAYS[done_at.weekday()]} {done_at:%d.%m}"


def _kyiv(moment: Optional[datetime]) -> Optional[datetime]:
    from app.business_day import BUSINESS_TIMEZONE

    if moment is None or BUSINESS_TIMEZONE is None:
        return moment
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(BUSINESS_TIMEZONE)


def board_view(db: Session, now: Optional[datetime] = None, cards=None) -> BoardView:
    """Картки для табло з того самого джерела, що й екран печей
    (`furnace.strip_cards`). `cards` — лише для тестів."""
    from app.services.furnace import strip_cards

    now = now or business_now()
    source = strip_cards(db) if cards is None else cards
    view = BoardView(now=now)
    nearest: Optional[tuple[datetime, str]] = None
    stamps: list[datetime] = []
    for card in source:
        state = card.state
        name = card.target.name
        if card.has_problem:
            view.cards.append(BoardCard(name=name, kind="bad", note="Табло печі не читається — число не вгадуємо"))
            continue
        temp = state.temp_c if state is not None else None
        if card.has_data and state is not None and state.captured_at is not None:
            stamp = _kyiv(state.captured_at)
            if stamp is not None:
                stamps.append(stamp)
        if card.is_running:
            item = BoardCard(name=name, kind="run", temp=temp)
            done_at = state.done_at if state is not None else None
            if state is not None and done_at is not None:
                item.open_day = _open_day(done_at, now)
                item.open_at = f"{done_at:%H:%M}"
                item.done_iso = done_at.isoformat()
                item.remaining = state.remaining_text
                if nearest is None or done_at < nearest[0]:
                    nearest = (done_at, name)
            else:
                item.note = "Час відкриття з табло не прочитано"
            view.cards.append(item)
        elif card.is_idle:
            view.cards.append(BoardCard(name=name, kind="idle", temp=temp))
        else:
            view.cards.append(BoardCard(name=name, kind="bad", temp=None, note="Стан печі не прочитано"))
    if nearest is not None:
        done_at, name = nearest
        view.nearest_name = name
        view.nearest_at = f"{done_at:%H:%M}"
        view.nearest_day = _open_day(done_at, now)
        view.nearest_iso = done_at.isoformat()
    if stamps:
        view.as_of = min(stamps).strftime("%H:%M")
    return view


# ── Сервер табло ────────────────────────────────────────────────────────────


@dataclass
class BoardStatus:
    listening: bool = False
    since: Optional[datetime] = None
    error: Optional[str] = None


_status = BoardStatus()
_status_lock = threading.Lock()


def status_snapshot() -> BoardStatus:
    with _status_lock:
        return BoardStatus(**_status.__dict__)


def _set_status(**changes) -> None:
    with _status_lock:
        for name, value in changes.items():
            setattr(_status, name, value)


def board_worker(stop_event: threading.Event, app_factory) -> None:
    """Тримає сервер табло ввімкненим рівно тоді, коли його ввімкнено в
    налаштуваннях. Перевіряє раз на 5 с — вимкнути табло кнопкою означає
    закрити порт за кілька секунд, без рестарту застосунку."""
    import uvicorn

    from app.db import SessionLocal

    import time

    server = None
    thread = None
    # Порт зайнятий — не долбати його кожні 5 с (і не засмічувати лог):
    # наступна спроба через хвилину.
    retry_at = 0.0
    if stop_event.wait(3):
        return
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                wanted = board_enabled(db) and bool(board_token(db))
        except Exception:  # noqa: BLE001 — сторож табло не має падати
            logger.exception("Табло пічок: не вдалось прочитати налаштування")
            wanted = False
        running = thread is not None and thread.is_alive()
        if thread is not None and not running and status_snapshot().error and not retry_at:
            retry_at = time.monotonic() + 60
        if wanted and not running and time.monotonic() >= retry_at:
            retry_at = 0.0
            try:
                # `log_config=None` — обов'язково, як у головного сервера
                # (`windows_launcher`). Збірка без консолі має `sys.stdout =
                # None`, і стандартний конфіг логів uvicorn падає на
                # `sys.stdout.isatty()` ще в конструкторі Config: у 0.15.5
                # табло через це не відкрило порт жодного разу (11.09.26,
                # ERR_CONNECTION_REFUSED), а на dev із консоллю все працювало.
                # До того ж той конфіг переписав би файловий лог KuubMill.
                config = uvicorn.Config(
                    app_factory(), host="0.0.0.0", port=BOARD_PORT,
                    lifespan="off", log_level="warning", access_log=False,
                    log_config=None,
                )
                # Сервер живе в НЕ головному потоці. Сигнали uvicorn (0.52)
                # тоді сам не чіпає — `capture_signals` перевіряє головний потік.
                server = uvicorn.Server(config)
            except Exception as exc:  # noqa: BLE001 — сторож табло не має падати
                logger.exception("Табло пічок: не вдалось підготувати сервер")
                _set_status(listening=False, error=f"не запустилось: {str(exc)[:160]}")
                retry_at = time.monotonic() + 60
                stop_event.wait(5)
                continue
            thread = threading.Thread(target=_serve, args=(server,), name="kuubmill-furnace-board-http", daemon=True)
            thread.start()
        elif not wanted and running and server is not None and thread is not None:
            server.should_exit = True
            thread.join(timeout=10)
            _set_status(listening=False, since=None, error=None)
        stop_event.wait(5)
    if server is not None:
        server.should_exit = True
    _set_status(listening=False)


def _serve(server) -> None:
    _set_status(listening=True, since=datetime.now(), error=None)
    try:
        server.run()
        if not server.started:
            # uvicorn не піднявся — найчастіше порт уже зайнятий.
            _set_status(listening=False, error=f"порт {BOARD_PORT} зайнятий іншою програмою")
            return
    except SystemExit:
        _set_status(listening=False, error=f"порт {BOARD_PORT} зайнятий іншою програмою")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Табло пічок: сервер впав")
        _set_status(listening=False, error=str(exc)[:200])
        return
    _set_status(listening=False)
