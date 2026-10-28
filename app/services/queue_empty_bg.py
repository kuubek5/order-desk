"""Фон порожньої черги — вибір оператора (власник 28.10.26).

Порожня черга малює кадр верстата на всю картку (`_queue_rows.html`,
`.empty-video-bg`). Досі кадр був один — світле фото диска, — і в темі
«Аврора» (майже чорне індиго) він вибивався світлою плямою. Тепер оператор
обирає фон у шестерні вигляду черги.

Значення на акаунті (`users.queue_empty_bg`):
* "" — «За темою» (дефолт): фон підбирається під тему оператора
  (`THEME_DEFAULT`). Порожній рядок у БД = нічого не обрано; на дроті канон
  має ВЛАСНЕ слово `AUTO` — порожнє поле сервер читає як «поля не було»
  (CLAUDE.md §14, «Порожнє поле»).
* ключ з `BACKGROUNDS` — конкретний кадр незалежно від теми;
* `NONE` — без кадру, лише картка.

Кадри — перефарбовані під палітри тем справжні фото наших верстатів
(design/refs → design/kuubmill_queue-empty-*_20261028.jpeg, промпти в .log.md
поруч), не вигадані сцени.
"""

from __future__ import annotations

from dataclasses import dataclass

AUTO = "auto"
NONE = "none"

_DIR = "img/queue-bg"


@dataclass(frozen=True)
class Background:
    key: str
    label: str
    # Шлях відносно /static — для `static_ver` і src.
    file: str
    thumb: str


def _bg(key: str, label: str, file: str | None = None) -> Background:
    return Background(key, label, file or f"{_DIR}/{key}.jpg", f"{_DIR}/{key}-thumb.jpg")


# Порядок = порядок мініатюр у шестерні.
BACKGROUNDS: tuple[Background, ...] = (
    # Те саме фото, що було єдиним до 28.10.26 — лишається на місці.
    _bg("photo", "Фото диска (як було)", "video/queue-empty-poster.jpg"),
    _bg("aurora-1", "Аврора · диск"),
    _bg("aurora-2", "Аврора · шпиндель"),
    _bg("forge-1", "Кузня · шпиндель"),
    _bg("forge-2", "Кузня · диск"),
    _bg("teal-1", "Бірюза · камера"),
    _bg("teal-2", "Бірюза · верстат"),
)
_BY_KEY = {bg.key: bg for bg in BACKGROUNDS}

# «За темою»: тема оператора → кадр. Тема "" (стара БД) = forge, як у ui_prefs.
THEME_DEFAULT = {
    "forge": "forge-2",
    "aurora": "aurora-1",
    "teal": "teal-2",
}

#: Що приймає роут: «за темою», без фону або будь-який кадр.
CHOICES = frozenset({AUTO, NONE, *_BY_KEY})


def stored_value(choice: str) -> str:
    """Слово з дроту → значення в БД. «За темою» зберігається порожнім."""
    return "" if choice == AUTO else choice


def theme_default(theme: str | None) -> Background:
    """Кадр «за темою». Тема "" (стара БД) або невідома = forge, як у ui_prefs."""
    return _BY_KEY[THEME_DEFAULT.get(theme or "forge", THEME_DEFAULT["forge"])]


def resolve(stored: str | None, theme: str | None) -> Background | None:
    """Який кадр показати. None — без фону.

    Невідоме значення (кадр прибрали з набору, ручна правка БД) поводиться як
    «за темою», а не як «без фону»: зникнути має лише свідомо вимкнене."""
    value = stored or ""
    if value == NONE:
        return None
    if value in _BY_KEY:
        return _BY_KEY[value]
    return theme_default(theme)


def choice_of(stored: str | None) -> str:
    """Що підсвітити в шестерні: збережене слово або `AUTO`."""
    value = stored or ""
    return value if value in CHOICES else AUTO
