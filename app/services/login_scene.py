"""Сцена екрана входу: що рухається за карткою логіну (власник 14.11.26).

Адміністратор обирає ОДНУ сцену (Налаштування → Робочий процес → «Екран
входу»). Типово — `video`, тобто екран лишається таким, яким був: відео рук
оператора над диском. Решту чотири малює `static/js/login_scenes.js`:

* `freza`  — диск, фреза обводить обриси робіт (Canvas 2D);
* `tochky` — хмара точок: коронка → KM → диск (Canvas 2D, своя 3D-проєкція);
* `svitlo` — рідке світло (WebGL-шейдер; без WebGL — нерухомий градієнт);
* `pult`   — пульт верстата: креслення лініями, оберти шпинделя (SVG).

Це лише тло й реакція на введення. Форма входу, перевірка пароля, обмеження
спроб і прихована реєстрація від сцени не залежать. Невідоме значення в базі
читається як `video` — вхід не має ламатись через налаштування вигляду.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.settings_store import get_setting, set_setting

KEY = "login_scene"
DEFAULT = "video"

# (ключ, назва, опис для адміна)
SCENES: tuple[tuple[str, str, str], ...] = (
    ("video", "Відео з цеху", "Руки оператора над щойно відфрезерованим диском. Так екран виглядає зараз."),
    ("freza", "Фреза", "Диск, який фреза обводить по обрисах робіт: світний слід, іскри, координати. "
                       "Поки вводиш — фреза прискорюється."),
    ("tochky", "Точки", "Кілька тисяч точок збираються в коронку, потім у знак KM, потім у диск. "
                        "Хмару можна крутити мишею."),
    ("svitlo", "Світло", "Повільне сяйво, яке рахує відеокарта. Найспокійніша сцена. "
                         "На ПК без підтримки WebGL буде нерухомий градієнт."),
    ("pult", "Пульт", "Вхід як запуск верстата: креслення диска малюється лініями, оберти шпинделя "
                      "ростуть з кожною літерою пароля."),
)
VALID = frozenset(key for key, _title, _hint in SCENES)


def normalize(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in VALID else DEFAULT


def current(db: Session) -> str:
    return normalize(get_setting(db, KEY))


def save(db: Session, value: object) -> str:
    """Зберігає сцену; невідома назва — ValueError (форма шле лише відомі)."""
    text = str(value or "").strip().lower()
    if text not in VALID:
        raise ValueError(f"невідома сцена входу: {text!r}")
    set_setting(db, KEY, text)
    return text


def title_of(key: str) -> str:
    return next((title for k, title, _hint in SCENES if k == key), SCENES[0][1])
