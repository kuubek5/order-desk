"""Знак застосунку: файли, які малює `design/brandmark.py`, мусять бути тими
самими, які читає збірка.

Приклад із життя (30.08.26): після перейменування Order Desk → KuubMill
`.spec`, `.iss` і launcher поїхали на `assets/kuubmill.ico`, а генератор далі
писав `assets/orderdesk.ico`. Обидва файли існували, збірка не падала, тести
були зелені — просто будь-яка правка форми знака НЕ доїжджала б до exe, трею
та ярлика. Мовчазний розрив, який видно лише коли хтось спробує перемалювати
логотип.

Тому тут перевіряється не «файл існує», а що ім'я в генераторі і в кожному
споживачі — одне.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BRANDMARK = ROOT / "design" / "brandmark.py"
SPEC = ROOT / "KuubMill.spec"
ISS = ROOT / "installer" / "KuubMill.iss"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_generator_writes_the_icon_the_build_reads():
    """Ім'я .ico збігається в генераторі, .spec і .iss."""
    name_match = re.search(r'APP_ICON_NAME\s*=\s*"([^"]+)"', _read(BRANDMARK))
    assert name_match, "у brandmark.py немає APP_ICON_NAME — ім'я знову розсіяне по файлу"
    icon = name_match.group(1)

    assert (ROOT / "assets" / icon).exists(), f"assets/{icon} не існує — збірці нема що класти"

    spec = _read(SPEC)
    assert f"assets/{icon}" in spec, f".spec не згадує assets/{icon}"

    iss = _read(ISS)
    assert icon in iss, f".iss не згадує {icon}"

    # І навпаки: у збірці не має лишитись іншого .ico, бо тоді знак роздвоївся б.
    for text, where in ((spec, "KuubMill.spec"), (iss, "KuubMill.iss")):
        others = {m.group(0) for m in re.finditer(r"[\w.-]+\.ico", text)} - {icon}
        assert not others, f"{where} посилається ще й на {sorted(others)}"


def test_brandmark_targets_exist():
    """Решта комплекту знака — на місці й не порожня."""
    for rel in (
        "app/static/img/logo-kmill.svg",
        "app/static/favicon.ico",
        "app/static/img/app-icon.png",
        "installer/wizard-large.bmp",
        "installer/wizard-small.bmp",
    ):
        path = ROOT / rel
        assert path.exists(), f"{rel} немає"
        assert path.stat().st_size > 0, f"{rel} порожній"


def test_generator_root_is_not_a_hardcoded_drive():
    """Корінь виводиться з розташування файлу.

    Був зашитий `P:\\AI-Projects\\CRM_Laba`; проєкт лежить на мережевій шарі,
    і на машині без цієї букви диска генератор писав би повз репозиторій —
    або падав.
    """
    text = _read(BRANDMARK)
    assert "pathlib.Path(__file__)" in text
    assert "AI-Projects" not in text, "у brandmark.py лишився зашитий шлях"


@pytest.mark.parametrize("rel", ["design/brandmark.py"])
def test_generator_compiles(rel):
    compile(_read(ROOT / rel), rel, "exec")
