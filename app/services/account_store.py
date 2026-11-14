"""Памʼять налаштувань вигляду в акаунті оператора (власник 14.11.26).

Рейка, бокові панелі черги, ширини стовпців, вигляд статистики, STL-перегляд…
раніше жили лише в localStorage браузера (`KMStore`, `storage.js`) — на іншому
ПК чи після очищення браузера оператор бачив усе скинутим. Тепер дозволені
ключі дзеркаляться в `User.ui_store` (JSON) і при кожному відкритті сторінки
браузер бере значення акаунта.

Лише ключі з `SYNCED_KEYS`: «побачене» (`queueSeenIds`), охолодження кнопок,
режими редагування й робочі позначки — НЕ налаштування, у акаунт не їдуть.
Положення вікна STL (`stl-preview-x/y`) теж ні: на іншому моніторі воно
вилізло б за край. Перелік дзеркалить `storage.js` (`SYNCED`), сторож —
`tests/test_account_store.py`.
"""

from __future__ import annotations

import json
from typing import Any

SYNCED_KEYS = (
    "railCollapsed",
    "sismaOpen",
    "queueColWidths",
    "furnaceSideOpen",
    "machineSideOpen",
    "qsideCollapsed",
    "statsDayView",
    "accTab",
    "discsTab",
    "mail.attlistWide",
    "mailMirrorCollapsed",
    "mail.showAll",
    "stl-preview-max",
    "stl-preview-light",
    "stl-preview-spin-speed",
    "stl-gallery-max",
)
MAX_VALUE_CHARS = 4000


def load(user: Any) -> dict[str, str]:
    """Збережені значення акаунта; зіпсований JSON = порожньо, не помилка."""
    try:
        data = json.loads(getattr(user, "ui_store", "") or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if k in SYNCED_KEYS and v is not None}


def put(user: Any, key: str, value: str | None) -> bool:
    """Записати (або прибрати при `None`) один ключ. False — ключ не дозволено
    чи значення завелике; тоді акаунт не змінюється."""
    if key not in SYNCED_KEYS:
        return False
    if value is not None and len(value) > MAX_VALUE_CHARS:
        return False
    data = load(user)
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    user.ui_store = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return True
