"""Палітра Ctrl+K і бічна рейка мусять вести в ті самі місця.

Це два незалежні переліки екранів: рейка — розмітка в `_topbar_nav.html`,
палітра — список `COMMANDS` у `app/routers/palette.py`. Новий екран додають в
одне з них і забувають про друге: тоді або палітра веде туди, чого в меню
немає, або меню має пункт, якого не знайти пошуком. Обидва варіанти оператор
помічає не одразу, тож тримаємо сторожа (ревʼю 07.09.26, C.10).

Звести їх в одне джерело було б краще, але рейка малює ще й іконки, підказку
Ctrl K, значки-лічильники й підсвітку активного пункту — переписувати її під
спільний реєстр означало б рухати верстку заради тесту.
"""

import re
from pathlib import Path

from app.routers.palette import COMMANDS

RAIL = Path(__file__).resolve().parent.parent / "app" / "templates" / "_topbar_nav.html"

# Свідома різниця: у рейці «Мій акаунт» — це підпис користувача внизу, окремим
# стилем, а не пункт навігації. У палітрі він потрібен як звичайний екран.
PALETTE_ONLY = {"/account"}


def _rail_hrefs() -> set[str]:
    html = RAIL.read_text(encoding="utf-8")
    hrefs = set()
    for match in re.finditer(r"<a\s+href=\"(/[^\"]*)\"([^>]*)>", html, re.S):
        href, rest = match.group(1), match.group(2)
        if "rail-nav-item" not in rest:
            continue
        if "rail-sset" in rest:
            continue  # вкладки всередині Налаштувань — не окремі екрани
        hrefs.add(href.split("#")[0])
    return hrefs


def _palette_hrefs() -> set[str]:
    return {item["href"] for item in COMMANDS}


def test_every_rail_screen_is_findable_in_the_palette():
    missing = _rail_hrefs() - _palette_hrefs()
    assert not missing, (
        "У рейці є пункти, яких немає в палітрі Ctrl+K: "
        + ", ".join(sorted(missing))
        + " — додайте їх у COMMANDS (app/routers/palette.py)"
    )


def test_palette_does_not_invent_screens_the_menu_lacks():
    extra = _palette_hrefs() - _rail_hrefs() - PALETTE_ONLY
    assert not extra, (
        "Палітра веде туди, чого немає в меню: "
        + ", ".join(sorted(extra))
        + " — або додайте пункт у рейку, або внесіть виняток у PALETTE_ONLY"
    )


def test_the_rail_is_actually_parsed():
    """Сторож самого сторожа: якщо розмітка рейки зміниться так, що регулярка
    перестане щось знаходити, обидва тести вище стануть зеленими й порожніми."""
    assert len(_rail_hrefs()) >= 10
