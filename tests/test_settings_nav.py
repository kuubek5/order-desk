"""Сторож реєстру меню налаштувань (`app/services/settings_nav.py`).

Меню, гейти включень у `settings.html`, права роутів і палітра Ctrl+K тепер
читають ОДИН реєстр. Ці тести стережуть саме стик: пункт без секції, секція без
пункту, ключ без плити стану, розділ, який меню показує, а роут забороняє.

Знімок складу реєстру (останній тест) міняється СВІДОМО — як
`tests/route_inventory.txt`: якщо він упав, спершу спитай себе, чи не зник
пункт, якого хтось шукатиме очима на екрані.
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.settings_nav import (
    ADMIN_ROLE,
    ITEMS,
    NAV,
    can_edit,
    can_see,
    nav_payload,
    visible_nav,
)
from app.services.settings_status import build_slabs

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
ROUTES = ROOT / "tests" / "route_inventory.txt"

ADMIN = SimpleNamespace(role=ADMIN_ROLE, username="admin")
OPERATOR = SimpleNamespace(role="оператор", username="operator")
# Роль, якої ще не існує в акаунтах: реєстр має відповісти на неї так само, як
# на оператора, без жодного нового `{% if %}` — це і є обіцянка «ролі як дані».
LOGIST = SimpleNamespace(role="логіст", username="logist")


def _section_keys_in_templates() -> set[str]:
    """`data-sec` усіх секцій, які реально рендеряться на /settings."""
    keys: set[str] = set()
    for path in TEMPLATES.glob("_settings_*.html"):
        text = io.open(path, encoding="utf-8").read()
        for match in re.finditer(r'scon-sec[^>]*data-sec="([^"]+)"', text):
            keys.add(match.group(1))
        for match in re.finditer(r'data-sec="([^"]+)"[^>]*scon-sec', text):
            keys.add(match.group(1))
    return keys


def _tab_keys_in_templates() -> set[str]:
    """`data-tab` панелей-вкладок: розділ живе всередині іншого розділу
    («Копії таблиці» в Google Таблиці, «Скачування вкладень» у Пошті,
    «Сповіщення» — у кабінеті)."""
    keys: set[str] = set()
    for path in list(TEMPLATES.glob("_settings_*.html")) + [TEMPLATES / "account.html"]:
        text = io.open(path, encoding="utf-8").read()
        for match in re.finditer(r'stand-tabpane[^>]*data-tab="([^"]+)"', text):
            keys.add(match.group(1))
    return keys


def _known_paths() -> set[str]:
    lines = io.open(ROUTES, encoding="utf-8").read().split("\n")
    return {line.split(" ", 1)[1].strip() for line in lines if line.strip() and " " in line}


# ── 1. Кожен пункт меню кудись веде ─────────────────────────────────────


def test_every_nav_href_resolves():
    sections = _section_keys_in_templates() | _tab_keys_in_templates()
    paths = _known_paths()
    broken: list[str] = []
    for item in ITEMS.values():
        if item.kind == "section":
            if item.key not in sections:
                broken.append(f"{item.key}: немає ні секції, ні вкладки з таким ключем")
            if not item.href.startswith("/settings#"):
                broken.append(f"{item.key}: секція має вести на /settings#<ключ>")
        else:
            # У сторінки може бути якір на вкладку (/account#notifications) —
            # роут перевіряємо без нього.
            path = item.href.split("#", 1)[0]
            if path not in paths:
                broken.append(f"{item.key}: {path} немає в route_inventory.txt")
    assert not broken, chr(10).join(broken)


def test_tab_items_point_at_a_real_host():
    """`parent` мусить називати розділ, який справді існує в реєстрі —
    інакше меню вело б у порожнечу, а вкладку ніхто не показав би."""
    for item in ITEMS.values():
        if item.parent:
            assert item.parent in ITEMS, f"{item.key}: господар «{item.parent}» поза реєстром"
            assert item.parent != item.key


def test_every_tab_pane_is_registered():
    """Панель-вкладка без пункту меню недосяжна так само, як осиротіла секція."""
    orphans = sorted(_tab_keys_in_templates() - set(ITEMS))
    assert not orphans, f"вкладки поза реєстром меню: {orphans}"


# ── 2. Кожна секція екрана досяжна з меню ───────────────────────────────


def test_every_settings_section_is_in_registry():
    """Секція в DOM без пункту в меню = кнопка, до якої не дійти."""
    orphans = sorted(_section_keys_in_templates() - set(ITEMS))
    assert not orphans, f"секції поза реєстром меню: {orphans}"


def test_every_section_item_has_a_status_slab():
    """Розділ без плити стану виглядає зламаним — плита є в кожного."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db import Base

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        slabs = set(build_slabs(db, {}))
    missing = sorted(
        item.key for item in ITEMS.values() if item.kind == "section" and item.key not in slabs
    )
    assert not missing, f"розділи без плити стану в build_slabs: {missing}"


# ── 3. Меню й роути домовились про ролі ─────────────────────────────────


def _gate_keys_by_route() -> dict[str, str]:
    """Які ключі реєстру згадують роути пакета налаштувань."""
    found: dict[str, str] = {}
    for path in (ROOT / "app" / "routers" / "settings").glob("*.py"):
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("require_settings_edit", "can_edit"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found[arg.value] = path.name
    return found


def test_route_gates_use_registry_keys():
    """Роут, що гейтиться неіснуючим ключем, мовчки пускав би всіх або нікого."""
    unknown = {key: mod for key, mod in _gate_keys_by_route().items() if key not in ITEMS}
    assert not unknown, f"гейти з ключами поза реєстром: {unknown}"


def _mail_filter_route_bodies() -> dict[str, str]:
    """Джерело кожного `/mail/filter...` роута в `app/routers/mail.py`.

    Дешева сітка проти регресу F3 (аудит 06.09.26): ці роути гейтяться через
    `can_edit(user, "mail-filters")`, бо оператор редагує «Джерела робіт»
    нарівні з адміном (рішення власника). Прямий `user.role != "адмін"` —
    старий, жорсткіший гейт — не має тихо повернутись в жоден з них.
    """
    path = ROOT / "app" / "routers" / "mail.py"
    source = io.open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    bodies: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "attr", None) in
                    ("get", "post", "put", "delete", "patch")):
                continue
            route_path = next(
                (arg.value for arg in dec.args if isinstance(arg, ast.Constant)
                 and isinstance(arg.value, str)),
                None,
            )
            if route_path and route_path.startswith("/mail/filter"):
                segment = ast.get_source_segment(source, node)
                bodies[route_path] = segment or ""
    return bodies


def test_mail_filter_routes_never_hardcode_admin_role():
    bodies = _mail_filter_route_bodies()
    assert bodies, "жодного /mail/filter роута не знайдено — перевір шлях/декоратор"
    offenders = {
        path: body for path, body in bodies.items()
        if 'role != "адмін"' in body or "role != 'адмін'" in body
    }
    assert not offenders, (
        f"пряма перевірка ролі замість can_edit(user, \"mail-filters\"): {sorted(offenders)}"
    )


@pytest.mark.parametrize("user", [OPERATOR, LOGIST], ids=["оператор", "логіст"])
def test_non_admin_sees_only_open_sections(user):
    visible = {item.key for group in visible_nav(user) for item in group.items}
    assert "operators" not in visible, "розділ «Оператори» лишається адмінським"
    assert "sections" not in visible, "«Блокування розділів» лишається адмінським"
    assert "license" not in visible and "backup" not in visible
    # Рішення власника 06.09.26: цех бачить і редагує свої джерела й обладнання.
    assert {"sheets", "imap", "paths", "furnaces", "machines"} <= visible


def test_admin_sees_everything():
    visible = {item.key for group in visible_nav(ADMIN) for item in group.items}
    assert visible == set(ITEMS)


def test_can_edit_matches_can_see():
    """Не можна редагувати те, чого не видно — інакше форма без входу в неї."""
    for key in ITEMS:
        for user in (OPERATOR, LOGIST):
            if can_edit(user, key):
                assert can_see(user, key), key


def test_unknown_key_is_admin_only():
    """Новий розділ, який забули внести в реєстр, не відкривається сам."""
    assert can_edit(ADMIN, "розділ-якого-нема")
    assert not can_edit(OPERATOR, "розділ-якого-нема")
    assert not can_see(OPERATOR, "розділ-якого-нема")


# ── 4. Порожніх заголовків немає ────────────────────────────────────────


@pytest.mark.parametrize("user", [ADMIN, OPERATOR, LOGIST], ids=["адмін", "оператор", "логіст"])
def test_no_empty_groups(user):
    for group in visible_nav(user):
        assert group.items, f"група «{group.title}» без пунктів для {user.role}"


def test_palette_payload_matches_menu():
    """Ctrl+K шукає рівно те, що є в рейці — не більше й не менше."""
    for user in (ADMIN, OPERATOR):
        menu = [item.key for group in visible_nav(user) for item in group.items]
        palette = [row["key"] for row in nav_payload(user)]
        assert menu == palette


def test_palette_finds_words_that_are_not_in_labels():
    """«export», «оновлення», «ліцензія» — те, що люди справді набирають."""
    rows = nav_payload(ADMIN)
    haystack = {
        row["key"]: (row["label"] + " " + " ".join(row["keywords"])).lower() for row in rows
    }
    assert "export" in haystack["paths"]
    assert "оновлення" in haystack["update"]
    assert "профайлер" in haystack["perf"]
    assert "ключ" in haystack["license"]


# ── 5. Знімок складу реєстру ────────────────────────────────────────────

# Ключ → (група, хто бачить, хто редагує). Міняти СВІДОМО: рядок тут — це
# відповідь на питання «а що бачить оператор», яку інакше довелось би збирати
# з семи шаблонів.
NAV_SNAPSHOT = {
    "account": ("mine", "усі", "адмін"),
    "notifications": ("mine", "усі", "усі"),
    "state": ("mine", "усі", "адмін"),
    "journal": ("mine", "усі", "адмін"),
    "update": ("mine", "усі", "адмін"),
    "sheets": ("sources", "усі", "усі"),
    "sheet-backup": ("sources", "усі", "усі"),
    "imap": ("sources", "усі", "усі"),
    "paths": ("sources", "усі", "усі"),
    "mail-download": ("sources", "усі", "усі"),
    "mail-filters": ("sources", "усі", "усі"),
    "materials": ("sources", "усі", "усі"),
    "furnaces": ("equipment", "усі", "усі"),
    "machines": ("equipment", "усі", "усі"),
    "operators": ("people", "адмін", "адмін"),
    "sections": ("people", "адмін", "адмін"),
    "handout": ("workflow", "адмін", "адмін"),
    "sync-journal": ("service", "адмін", "адмін"),
    "backup": ("service", "адмін", "адмін"),
    "feedback": ("service", "адмін", "адмін"),
    "perf": ("service", "адмін", "адмін"),
    "license": ("service", "адмін", "адмін"),
}


def test_registry_snapshot():
    actual = {
        item.key: (
            group.key,
            "усі" if item.roles is None else "адмін",
            "усі" if item.edit_roles is None else "адмін",
        )
        for group in NAV
        for item in group.items
    }
    assert actual == NAV_SNAPSHOT
