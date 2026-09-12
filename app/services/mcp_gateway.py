"""Доступ до `/mcp` по мережі — окремий слухач, що вмикається з налаштувань.

Навіщо окремий порт, а не гейт на головному застосунку. Головна CRM слухає
`127.0.0.1` і мусить так і лишитись: адреса вибирається один раз при старті
`uvicorn`, тож «увімкнути доступ» на ній означало б перезапуск усього
застосунку — обрив синку, пошти й опитування печей посеред робочого дня. Тому
тут живе ДРУГИЙ, крихітний слухач (`GATEWAY_PORT`), у якому фізично немає
нічого, крім `POST /mcp`: ні сторінок, ні входу, ні статики. Рівно той самий
прийом, що в табло печей (`app/services/furnace_board.py`) — і та сама причина
тримати їх окремо: вимкнув перемикач → порт зник із мережі за кілька секунд, а
не «відповідає відмовою».

Межа доступу тут — ТОКЕН, не loopback: сенс слухача саме в тому, щоб пустити
запит з іншої машини (домашній ПК через тунель WireGuard). Усе, що віддається,
лише читається (`app/services/mcp_tools.py`), тож найгірше, що дає викрадений
токен, — читання стану цеху; змінити ним нічого не можна.

Токен показується ОДИН раз, коли його створюють. У базі він лежить як
налаштування, у Jinja-контекст не потрапляє (§14 «Секрети»): екран знає лише
«задано / не задано». Загубив — перевипускаєш, старий одразу мертвий.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import secrets
import threading
from typing import Optional

from sqlalchemy.orm import Session

from app.services.furnace_board import lan_addresses
from app.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

GATEWAY_PORT = 8011
"""8010 зайнятий табло печей, 8000/8002 — прод і dev самої CRM."""

ENABLED_KEY = "mcp_remote_enabled"
TOKEN_KEY = "mcp_remote_token"


# ── Налаштування ────────────────────────────────────────────────────────────


def gateway_enabled(db: Session) -> bool:
    return (get_setting(db, ENABLED_KEY) or "") == "1"


def gateway_token(db: Session) -> Optional[str]:
    value = (get_setting(db, TOKEN_KEY) or "").strip()
    return value or None


def has_token(db: Session) -> bool:
    """Чи задано токен — саме це показує екран, а не значення."""
    return gateway_token(db) is not None


def regenerate_token(db: Session) -> str:
    """Новий токен; старий одразу перестає працювати. Не комітить.

    32 байти, бо цей токен може приїхати з іншої машини, а не лише з локальної
    мережі, як посилання табло.
    """
    token = secrets.token_urlsafe(32)
    set_setting(db, TOKEN_KEY, token)
    return token


def token_matches(db: Session, candidate: str) -> bool:
    token = gateway_token(db)
    if not token:
        return False
    return secrets.compare_digest(token.encode(), (candidate or "").encode())


def gateway_addresses() -> list[str]:
    """Адреси цього ПК, за якими слухач видно. Адресу тунелю WireGuard Windows
    теж показує як звичайний інтерфейс, тож окремої логіки не треба."""
    return lan_addresses()


# ── Стан (для плити в налаштуваннях) ────────────────────────────────────────


@dataclass
class GatewayStatus:
    listening: bool = False
    since: Optional[datetime] = None
    error: Optional[str] = None


_status = GatewayStatus()
_status_lock = threading.Lock()


def status_snapshot() -> GatewayStatus:
    with _status_lock:
        return GatewayStatus(_status.listening, _status.since, _status.error)


def _set_status(**changes) -> None:
    with _status_lock:
        for key, value in changes.items():
            setattr(_status, key, value)


# ── Сервер слухача ──────────────────────────────────────────────────────────

# Пауза перед першою спробою: на старті застосунку база ще читається схемою й
# міграціями, і сторожу нема чого стукати в неї наввипередки.
_START_DELAY_SECONDS = 3.0


def gateway_worker(stop_event: threading.Event, app_factory) -> None:
    """Тримає слухач `/mcp` ввімкненим рівно тоді, коли його ввімкнено в
    налаштуваннях І задано токен. Перевіряє раз на 5 с — вимкнув перемикач,
    і порт зник із мережі за кілька секунд, без рестарту всієї CRM.

    Один у один сторож табло печей (`furnace_board.board_worker`): той прийом
    уже пережив прод, і розходитись їм нема з чого."""
    import time

    import uvicorn

    from app.db import SessionLocal

    server = None
    thread = None
    # Порт зайнятий — не долбати його кожні 5 с (і не засмічувати лог):
    # наступна спроба не раніше, ніж через хвилину.
    retry_at = 0.0
    if stop_event.wait(_START_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                wanted = gateway_enabled(db) and has_token(db)
        except Exception:  # noqa: BLE001 — сторож слухача не має падати
            logger.exception("Доступ /mcp по мережі: не вдалось прочитати налаштування")
            wanted = False
        running = thread is not None and thread.is_alive()
        if thread is not None and not running and status_snapshot().error and not retry_at:
            retry_at = time.monotonic() + 60
        if wanted and not running and time.monotonic() >= retry_at:
            retry_at = 0.0
            try:
                # `log_config=None` — не обговорюється. Прод зібраний без
                # консолі (`KuubMill.spec`, `console=False`), там
                # `sys.stdout is None`, і стандартний конфіг логів uvicorn
                # падає на `sys.stdout.isatty()` ще в конструкторі Config: у
                # 0.15.5 табло печей через це не відкрило порт ЖОДНОГО разу, а
                # на dev із консоллю все працювало. До того ж той конфіг
                # переписав би файловий лог KuubMill.
                config = uvicorn.Config(
                    app_factory(), host="0.0.0.0", port=GATEWAY_PORT,
                    lifespan="off", log_level="warning", access_log=False,
                    log_config=None,
                )
                # Сервер живе в НЕ головному потоці. Сигнали uvicorn тоді сам
                # не чіпає — `capture_signals` перевіряє головний потік.
                server = uvicorn.Server(config)
            except Exception as exc:  # noqa: BLE001 — сторож слухача не має падати
                logger.exception("Доступ /mcp по мережі: не вдалось підготувати сервер")
                _set_status(listening=False, error=f"не запустилось: {str(exc)[:160]}")
                retry_at = time.monotonic() + 60
                stop_event.wait(5)
                continue
            thread = threading.Thread(target=_serve, args=(server,), name="kuubmill-mcp-gateway-http", daemon=True)
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
            _set_status(listening=False, error=f"порт {GATEWAY_PORT} зайнятий іншою програмою")
            return
    except SystemExit:
        _set_status(listening=False, error=f"порт {GATEWAY_PORT} зайнятий іншою програмою")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Доступ /mcp по мережі: сервер впав")
        _set_status(listening=False, error=str(exc)[:200])
        return
    _set_status(listening=False)
