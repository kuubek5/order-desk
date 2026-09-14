"""Робота з інших ПК — головний застосунок на `0.0.0.0:8000` за перемикачем.

Навіщо. Один сервер, кілька браузерів: другий операторський ПК, адмін, що
править налаштування, поки за головним ПК працює оператор. Копія програми
лишається ОДНА (SQLite, синк таблиці, пошта, печі — одинаки; спільна база
через SMB = пошкоджений файл), а інші ПК відкривають адресу сервера в
браузері. Рішення власника 12.09.26: транспорт — HTTP у приватній мережі,
брандмауер лише для приватного профілю, адмінські дії по мережі теж.

Чим відрізняється від табло печей (8010) і MCP (8011). Ті два — окремі
крихітні слухачі, які сторож підіймає/гасить без рестарту. Тут інакше:
адреса ГОЛОВНОГО `uvicorn` вибирається один раз на старті
(`app/windows_launcher.py`), тож перемикач набуває чинності після
перезапуску застосунку — і роут перемикача цей перезапуск чесно робить сам
(`request_restart`), а екран доти показує «чекає перезапуску».

Межа доступу. Сесія та роль лишаються як були; додається перевірка адреси:
`is_trusted_request` пускає loopback завжди, а мережевого клієнта — лише
коли перемикач увімкнено І адреса приватна (RFC 1918 / тунель). Це другий
запобіжник поверх брандмауера: правило, написане надто широко, не відкриє
адмінські дії в чужу мережу.

Що по мережі НЕ працює й далі — дії, які відкривають щось на РОБОЧОМУ СТОЛІ
сервера («Відкрити теку» в Провіднику): з іншого ПК Провідник сервера не
видно. Такі роути замість відмови повертають шлях, і браузер копіює його в
буфер (`app/static/js/app.js`, `openFolderOrCopy`).
"""

from __future__ import annotations

import ipaddress
import threading
from typing import Callable, Optional

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services.furnace_board import lan_addresses
from app.settings_store import get_setting

APP_PORT = 8000
LOOPBACK_HOST = "127.0.0.1"
NETWORK_HOST = "0.0.0.0"

ENABLED_KEY = "network_access_enabled"
FIREWALL_RULE_NAME = "KuubMill"


# ── Налаштування ────────────────────────────────────────────────────────────


def access_enabled(db: Session) -> bool:
    return (get_setting(db, ENABLED_KEY) or "") == "1"


def desired_host(db: Session) -> str:
    """Адреса, на якій застосунок МАЄ слухати за налаштуванням."""
    return NETWORK_HOST if access_enabled(db) else LOOPBACK_HOST


# ── Що слухає зараз (реєструє лаунчер) ──────────────────────────────────────

_bound_host: Optional[str] = None
_lock = threading.Lock()


def set_bound_host(host: str) -> None:
    """Лаунчер каже, на якій адресі реально піднято `uvicorn`. Без цього
    екран не знає, чи зміна перемикача вже набула чинності."""
    global _bound_host
    with _lock:
        _bound_host = host


def bound_host() -> Optional[str]:
    with _lock:
        return _bound_host


def restart_pending(db: Session) -> bool:
    """Перемикач змінили, а сервер ще слухає стару адресу. `None` (dev, адреса
    не зареєстрована) — не «чекає», інакше dev вічно показував би банер."""
    current = bound_host()
    return current is not None and current != desired_host(db)


def network_listening() -> bool:
    return bound_host() == NETWORK_HOST


# ── Адреси, брандмауер ──────────────────────────────────────────────────────


def app_links() -> list[str]:
    """Адреси, які вводять у браузері інших ПК."""
    return [f"http://{address}:{APP_PORT}/" for address in lan_addresses()]


def firewall_command() -> str:
    """Дозволити вхідні на порт застосунку — ЛИШЕ приватний і доменний профілі.

    На відміну від MCP (`mcp_gateway.firewall_command`, усі профілі заради
    тунелю WireGuard), сюди ходять паролі й секрети відкритим текстом, тож
    у «Загальнодоступну» мережу (гостьовий Wi-Fi, готель з ноутбуком) порт
    не відкриваємо. Рішення власника 12.09.26.
    """
    return (
        f'netsh advfirewall firewall add rule name="{FIREWALL_RULE_NAME}" '
        f"dir=in action=allow protocol=TCP localport={APP_PORT} profile=private,domain"
    )


def firewall_remove_command() -> str:
    return f'netsh advfirewall firewall delete rule name="{FIREWALL_RULE_NAME}"'


# ── Гейт запиту ─────────────────────────────────────────────────────────────


def _client_address(request: Request) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    client = getattr(request, "client", None)
    if client is None:
        return None
    try:
        return ipaddress.ip_address(client.host)
    except (ValueError, AttributeError):
        return None


def is_private_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def is_trusted_request(request: Request, db: Session) -> bool:
    """Loopback — завжди. Мережевий клієнт — лише з увімкненим перемикачем і
    з приватної адреси. Публічна адреса не пускається ніколи, навіть із
    сесією адміна: брандмауер міг бути відкритий ширше, ніж треба."""
    address = _client_address(request)
    if address is None:
        return False
    if address.is_loopback:
        return True
    if not address.is_private or db is None:
        return False
    return access_enabled(db)


# ── Перезапуск застосунку ───────────────────────────────────────────────────

_restarter: Optional[Callable[[], None]] = None


def register_restarter(fn: Callable[[], None]) -> None:
    """Лаунчер реєструє функцію «зупинитись і піднятись знову». Сервіс роутери
    не імпортує, тож звʼязок лише через цей хук (як `set_report_builder` у
    боті)."""
    global _restarter
    _restarter = fn


def can_restart() -> bool:
    return _restarter is not None


def request_restart() -> bool:
    """Попросити застосунок перезапуститись. `False` — нема кому (dev під
    `uvicorn` без лаунчера): тоді екран каже перезапустити руками."""
    if _restarter is None:
        return False
    _restarter()
    return True
