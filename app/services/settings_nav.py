"""Реєстр меню налаштувань — одне місце, де описано розділи, ролі й ключі.

**Навіщо.** Меню жило в розмітці `_topbar_nav.html` (сім окремих
`{% if user.role == 'адмін' %}`), гейт включень — у `settings.html`, гейт
роутів — у кожному роуті, а палітра `Ctrl+K` скрейпила DOM рейки. Чотири місця,
які мають збігатися руками; розсинхрон «меню показує, роут забороняє» ловився
лише очима. Тепер меню, ролі й права редагування описані тут, а рейка, гейти,
палітра й тести читають цей реєстр.

**Ролі — множина, не булеве.** `roles=None` означає «усі ролі»; інакше явний
перелік. Адмін бачить і редагує все ЗАВЖДИ (той самий принцип, що в
`section_gate`). Нова роль (логіст, технік) зʼявляється в акаунтах — і одразу
працює, без нового `{% if %}`.

**Бачити ≠ редагувати.** `roles` відповідає на «чи показувати пункт»,
`edit_roles` — на «чи показувати форми й пускати POST». Рішення власника
06.09.26: оператор редагує «Джерела робіт» і «Обладнання» нарівні з адміном
(включно з секретами), тож у цих пунктів `edit_roles=None`. Обслуговування,
люди й доступ лишаються адмінськими.

Новий розділ = рядок у `NAV` + партіал із таким самим `data-sec` + плита в
`settings_status.build_slabs`. Тести `tests/test_settings_nav.py` не пропустять
пункт без секції й секцію без пункту.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ADMIN_ROLE = "адмін"

ADMIN_ONLY = frozenset({ADMIN_ROLE})


@dataclass(frozen=True)
class NavItem:
    key: str
    """`data-sec` секції на /settings або slug окремої сторінки."""

    title: str
    href: str
    kind: str = "section"
    """`section` — свап на /settings; `page` — окрема сторінка (стрілка ↗)."""

    roles: Optional[frozenset] = None
    """Хто БАЧИТЬ пункт. None = усі ролі; адмін бачить завжди."""

    edit_roles: Optional[frozenset] = ADMIN_ONLY
    """Хто РЕДАГУЄ розділ. None = усі ролі; адмін редагує завжди."""

    icon: str = ""
    """Ключ макроса в `_nav_icons.html`; порожній = такий самий, як `key`."""

    keywords: tuple = ()
    """Синоніми для Ctrl+K: «export», «оновлення», «пароль»."""

    badge: Optional[str] = None
    """Ключ живого бейджа біля пункту (поки лише `sync_dot`)."""

    gate: bool = False
    """Пункт підпадає під «Блокування розділів» (`section_gate.SECTIONS`)."""

    parent: Optional[str] = None
    """Ключ розділу-господаря, якщо це його ВКЛАДКА, а не окрема секція.
    Пункт лишається в меню й у Ctrl+K: адреса `/settings#<ключ>` відкриває
    господаря одразу на потрібній вкладці (`settings_console.js`)."""

    @property
    def icon_key(self) -> str:
        return self.icon or self.key


@dataclass(frozen=True)
class NavGroup:
    key: str
    title: str
    items: tuple
    heading: bool = True
    """Чи малювати заголовок групи в рейці."""


# ── Реєстр ──────────────────────────────────────────────────────────────
# Порядок тут = порядок у рейці. Групування V2 «Моє / Адміністрування»
# (рішення власника 06.09.26): спершу поділ за КИМ, потім за ЧИМ.

NAV: tuple = (
    NavGroup(
        key="mine",
        title="Моє робоче місце",
        heading=False,
        items=(
            NavItem(
                key="account",
                title="Мій акаунт",
                href="/account",
                kind="page",
                keywords=("пароль", "літера", "вихід", "профіль"),
            ),
            NavItem(
                key="notifications",
                title="Сповіщення",
                href="/account#notifications",
                kind="page",
                edit_roles=None,
                parent="account",
                keywords=("спливні", "звук", "попап"),
            ),
            NavItem(
                key="state",
                title="Стан системи",
                href="/settings#state",
                keywords=("самоперевірка", "діагностика", "здоровʼя"),
            ),
            NavItem(
                key="journal",
                title="Журнал дій",
                href="/journal",
                kind="page",
                keywords=("історія", "хто зробив", "скасувати"),
            ),
            NavItem(
                key="update",
                title="Про застосунок",
                href="/settings#update",
                keywords=("версія", "оновлення", "changelog", "що нового"),
            ),
        ),
    ),
    NavGroup(
        key="sources",
        title="Джерела робіт",
        items=(
            NavItem(
                key="sheets",
                title="Google Таблиця",
                href="/settings#sheets",
                edit_roles=None,
                keywords=("google", "sheets", "синк", "таблиця", "oauth"),
            ),
            NavItem(
                key="sheet-backup",
                title="Копії таблиці",
                href="/settings#sheet-backup",
                edit_roles=None,
                parent="sheets",
                keywords=("знімок", "csv", "відновлення", "вкладки"),
            ),
            NavItem(
                key="imap",
                title="Пошта (IMAP)",
                href="/settings#imap",
                edit_roles=None,
                keywords=("ukr.net", "пошта", "пароль", "скринька"),
            ),
            NavItem(
                key="mail-download",
                title="Скачування вкладень",
                href="/settings#mail-download",
                edit_roles=None,
                parent="imap",
                keywords=("вкладення", "архів", "rar", "спул"),
            ),
            NavItem(
                key="paths",
                title="Шляхи папок",
                href="/settings#paths",
                edit_roles=None,
                keywords=("export", "sum3d", "cam-work", "тека", "мережа"),
            ),
            NavItem(
                key="mail-filters",
                title="Фільтри пошти",
                href="/settings#mail-filters",
                edit_roles=None,
                keywords=("правила", "не наша робота", "3d-друк", "категорії"),
            ),
            NavItem(
                key="materials",
                title="Матеріали",
                href="/settings/materials",
                kind="page",
                edit_roles=None,
                keywords=("цирконій", "пмма", "синоніми", "розпізнавання"),
            ),
        ),
    ),
    NavGroup(
        key="equipment",
        title="Обладнання",
        items=(
            NavItem(
                key="furnaces",
                title="Пічки",
                href="/settings#furnaces",
                edit_roles=None,
                keywords=("піч", "vnc", "austromat", "спікання"),
            ),
            NavItem(
                key="machines",
                title="Верстати",
                href="/settings#machines",
                edit_roles=None,
                keywords=("imes", "remicore", "sisma", "фрезер", "калібрування"),
            ),
            # «Заготовки» (тека CAM) звідси переїхали на окремий екран
            # «Нові диски» (/discs, 10.09.26): шлях до теки й «Перечитати»
            # тепер у його нижній смузі, поруч із результатом.
        ),
    ),
    NavGroup(
        key="workflow",
        title="Робочий процес",
        items=(
            NavItem(
                key="handout",
                title="Ранкова видача",
                href="/settings#handout",
                roles=ADMIN_ONLY,
                keywords=("qc", "чеклист", "знайдено", "видача", "звірка"),
            ),
        ),
    ),
    NavGroup(
        key="people",
        title="Люди й доступ",
        items=(
            NavItem(
                key="operators",
                title="Оператори",
                href="/settings#operators",
                roles=ADMIN_ONLY,
                keywords=("користувачі", "пароль", "доступ", "pin"),
            ),
            NavItem(
                key="sections",
                title="Блокування розділів",
                href="/settings#sections",
                roles=ADMIN_ONLY,
                keywords=("закрити розділ", "блокатор", "у розробці"),
            ),
        ),
    ),
    NavGroup(
        key="service",
        title="Обслуговування",
        items=(
            NavItem(
                key="sync-journal",
                title="Журнал синку",
                href="/journal/sync",
                kind="page",
                roles=ADMIN_ONLY,
                icon="sync",
                badge="sync_dot",
                keywords=("синхронізація", "помилки", "тіки"),
            ),
            NavItem(
                key="backup",
                title="Резервна копія",
                href="/settings#backup",
                roles=ADMIN_ONLY,
                keywords=("бекап", "експорт бази", "імпорт"),
            ),
            NavItem(
                key="feedback",
                title="Зворотний зв'язок",
                href="/settings/feedback",
                kind="page",
                roles=ADMIN_ONLY,
                keywords=("telegram", "бот", "скарги", "ідеї"),
            ),
            NavItem(
                key="perf",
                title="Швидкодія",
                href="/diag/perf",
                kind="page",
                roles=ADMIN_ONLY,
                keywords=("профайлер", "затримки", "повільно", "діагностика"),
            ),
            NavItem(
                key="license",
                title="Ліцензія",
                href="/license",
                kind="page",
                roles=ADMIN_ONLY,
                keywords=("ключ", "термін", "активація"),
            ),
            NavItem(
                key="mcp",
                title="Доступ по мережі",
                href="/settings#mcp",
                roles=ADMIN_ONLY,
                keywords=("mcp", "wireguard", "токен", "віддалений доступ", "8011", "тунель"),
            ),
        ),
    ),
)


# ── Пошук по реєстру ────────────────────────────────────────────────────

ITEMS: dict = {item.key: item for group in NAV for item in group.items}

GROUP_OF: dict = {item.key: group for group in NAV for item in group.items}


def _role(user) -> str:
    return getattr(user, "role", "") or ""


def _allowed(roles: Optional[frozenset], user) -> bool:
    """Адмін проходить завжди; None = усі ролі; інакше — за переліком."""
    role = _role(user)
    if role == ADMIN_ROLE:
        return True
    if roles is None:
        return True
    return role in roles


def can_see(user, key: str) -> bool:
    item = ITEMS.get(key)
    if item is None:
        return False
    return _allowed(item.roles, user)


def can_edit(user, key: str) -> bool:
    """Чи показувати форми/кнопки розділу й пускати його POST-и.

    Невідомий ключ = «ні для всіх, крім адміна»: новий розділ, який забули
    внести в реєстр, не має мовчки відкритися оператору.
    """
    item = ITEMS.get(key)
    if item is None:
        return _role(user) == ADMIN_ROLE
    if not _allowed(item.roles, user):
        return False
    return _allowed(item.edit_roles, user)


def visible_nav(user, db=None) -> list:
    """Групи з пунктами, які видно цьому користувачеві. Порожні групи випадають.

    `db` потрібна лише для пунктів із `gate=True` (блокування розділів) —
    поки таких немає, сесію не смикаємо взагалі.
    """
    from app.services.section_gate import blocked_for

    result = []
    for group in NAV:
        items = []
        for item in group.items:
            if not _allowed(item.roles, user):
                continue
            if item.gate and db is not None and blocked_for(db, user, item.key):
                continue
            items.append(item)
        if items:
            result.append(NavGroup(group.key, group.title, tuple(items), group.heading))
    return result


def nav_payload(user, db=None) -> list:
    """Плаский список для палітри Ctrl+K — те саме, що бачить рейка."""
    return [
        {
            "key": item.key,
            "label": item.title,
            "href": item.href,
            "sec": item.key if item.kind == "section" else "",
            "group": group.title,
            "kind": item.kind,
            "keywords": list(item.keywords),
        }
        for group in visible_nav(user, db)
        for item in group.items
    ]


__all__ = [
    "NAV",
    "ITEMS",
    "NavItem",
    "NavGroup",
    "ADMIN_ONLY",
    "ADMIN_ROLE",
    "can_see",
    "can_edit",
    "visible_nav",
    "nav_payload",
]
