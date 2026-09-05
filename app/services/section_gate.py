"""Стан розділів «у розробці / тестується» — доменна частина гейта.

Рома час від часу каже «зачини розділ X»: не-адміни мають бачити екран-блокатор
з одним із чотирьох артів (app/static/img/blockers), адмін — сам розділ плюс
тонкий банер із кнопкою «Відкрити для всіх». Стан живе в налаштуваннях
(`section_state:<розділ>`), тому відкрити розділ можна без деплою.

Значення стану: "open" або назва арту. Арт обирається під характер розділу
(див. README у теці артів): mill — активна розробка, blueprint — проєктується,
gauge — працює, але звіряємо, shutter — тимчасово зачинено.

HTTP-частина (побудова Response) — app/routers/section_gate.py.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.settings_store import get_setting, set_setting

OPEN = "open"
# Аудиторія блокатора: "*" — усі не-адміни (і майбутні ролі), інакше явний
# перелік ролей через кому. Адмін НІКОЛИ не потрапляє під блокатор.
AUDIENCE_ALL = "*"
ADMIN_ROLE = "адмін"

# Тексти екрана під кожен арт. Заголовок може містити <br>/<u> — рендериться
# як safe у шаблоні, тому сюди не потрапляє нічого з користувацького вводу.
VARIANTS: dict[str, dict[str, str]] = {
    "mill": {
        "chip": "у роботі",
        "title": "Розділ ще<br><u>фрезерується</u>",
        "sub": "Ми доробляємо цей екран. Він з'явиться в меню, щойно пройде "
               "перевірку на реальних роботах. Черга й видача працюють як завжди.",
    },
    "blueprint": {
        "chip": "проєктується",
        "title": "На кресленні",
        "sub": "Розділ проєктується. Доступний адміністраторам для перевірки; "
               "для решти з'явиться після затвердження.",
    },
    "gauge": {
        "chip": "тестується",
        "title": "Проходить<br>калібрування",
        "sub": "Розділ уже працює, але ми звіряємо його з реальними даними цеху, "
               "перш ніж відкрити для всіх.",
    },
    "shutter": {
        "chip": "роботи тривають",
        "title": "Зачинено",
        "sub": "на роботи",
    },
}

# Реєстр розділів, що вміють бути закритими. default — стан, поки адмін не
# змінив його через банер (тобто «щойно встановлений» застосунок уже закриває
# розділ, без міграції даних). Новий розділ = рядок тут + ключ у
# settings_store.PREFERENCE_KEYS + виклик гейта в його роуті.
SECTIONS: dict[str, dict[str, str]] = {
    "stats": {"title": "Статистика", "path": "/stats", "default": "gauge"},
}


def _key(section: str) -> str:
    return f"section_state:{section}"


def _aud_key(section: str) -> str:
    return f"section_audience:{section}"


def non_admin_roles(db: Session) -> list[str]:
    """Ролі, які можна закрити (усі, крім адміна) — з реальних акаунтів, тому
    нова роль зʼявляється в таргетингу сама, без правок коду."""
    rows = db.scalars(select(User.role).distinct()).all()
    return sorted(r for r in rows if r and r != ADMIN_ROLE)


def section_state(db: Session, section: str) -> str:
    """Поточний стан розділу; невідоме/зіпсоване значення → дефолт реєстру."""
    meta = SECTIONS[section]
    value = get_setting(db, _key(section))
    if value == OPEN or value in VARIANTS:
        return value
    return meta["default"]


def section_audience(db: Session, section: str) -> str | list[str]:
    """AUDIENCE_ALL («*») або перелік ролей. Порожньо/невідоме → усі."""
    raw = get_setting(db, _aud_key(section))
    if not raw or raw == AUDIENCE_ALL:
        return AUDIENCE_ALL
    roles = [r.strip() for r in raw.split(",") if r.strip() and r.strip() != ADMIN_ROLE]
    return roles or AUDIENCE_ALL


def set_section_state(db: Session, section: str, state: str) -> None:
    if section not in SECTIONS:
        raise KeyError(section)
    if state != OPEN and state not in VARIANTS:
        raise ValueError(state)
    set_setting(db, _key(section), state)


def set_section_audience(db: Session, section: str, roles) -> None:
    """roles: AUDIENCE_ALL, або список ролей. Порожній список = усі («*»)."""
    if section not in SECTIONS:
        raise KeyError(section)
    if roles == AUDIENCE_ALL or not roles:
        value = AUDIENCE_ALL
    else:
        clean = [r for r in roles if r and r != ADMIN_ROLE]
        value = AUDIENCE_ALL if not clean else ",".join(sorted(set(clean)))
    set_setting(db, _aud_key(section), value)


def sections_admin(db: Session) -> list[dict]:
    """Список усіх керованих розділів для картки в Налаштуваннях: назва, шлях,
    поточний стан і варіанти для select (open + чотири арти)."""
    variants = [(OPEN, "Відкрито для всіх")] + [(k, v["chip"]) for k, v in VARIANTS.items()]
    roles = non_admin_roles(db)
    out = []
    for section, meta in SECTIONS.items():
        state = section_state(db, section)
        audience = section_audience(db, section)
        all_roles = audience == AUDIENCE_ALL
        out.append({
            "section": section,
            "title": meta["title"],
            "path": meta["path"],
            "state": state,
            "is_open": state == OPEN,
            "variants": variants,
            # Ролі з відміткою «під блокатором»: за «*» — усі; інакше за переліком.
            "roles": [{"role": r, "on": all_roles or r in audience} for r in roles],
            "audience_all": all_roles,
        })
    return out


def is_admin(user) -> bool:
    return getattr(user, "role", None) == "адмін"


def blocked_for(db: Session, user, section: str) -> str | None:
    """Назва арту, якщо цьому користувачу розділ треба закрити, інакше None.

    Адмін не блокується ніколи. Аудиторія «*» ловить усіх не-адмінів; інакше —
    лише тих, чия роль у переліку."""
    state = section_state(db, section)
    if state == OPEN or is_admin(user):
        return None
    audience = section_audience(db, section)
    if audience == AUDIENCE_ALL or getattr(user, "role", None) in audience:
        return state
    return None


def admin_banner(db: Session, user, section: str) -> dict | None:
    """Дані банера для адміна: розділ закритий для інших, є що перемкнути."""
    state = section_state(db, section)
    if state == OPEN or not is_admin(user):
        return None
    meta = SECTIONS[section]
    return {
        "section": section,
        "title": meta["title"],
        "path": meta["path"],
        "state": state,
        "chip": VARIANTS[state]["chip"],
        "variants": [(k, v["chip"]) for k, v in VARIANTS.items()],
    }
