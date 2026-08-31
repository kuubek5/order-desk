"""Кадр родини матеріалу: одне джерело на всіх споживачів.

Кольори родин уже одного разу розійшлися між маркуванням і смугою матеріалу
в черзі — саме тому кадр оголошено ЗМІННОЮ `--mat-art` на класі родини, а не
картинкою на кожному елементі окремо. Споживачів два (мітка в бібліотеці
матеріалів і чіп у паспорті роботи), і прописані нарізно вони розійшлися б
так само.

Тут перевіряється три речі: змінна оголошена рівно в одному файлі, файли
кадрів існують, і жоден споживач не малює картинку повз змінну.
"""

import re
from pathlib import Path

from app.material_class import material_family_class

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "app" / "static" / "css"
FAMILIES = ["mat-zr", "mat-pmma", "mat-ti", "mat-slm", "mat-wax"]


def _css(name: str) -> str:
    return (CSS / name).read_text(encoding="utf-8")


def test_every_family_declares_its_art_once():
    """`--mat-art` оголошено для всіх п'яти родин і лише в одному файлі."""
    declaring = [p.name for p in CSS.glob("*.css") if "--mat-art:" in p.read_text(encoding="utf-8")]
    assert declaring == ["update_overlay.css"], f"змінна оголошена ще й у {declaring}"

    text = _css("update_overlay.css")
    for family in FAMILIES:
        assert re.search(rf"\.{family}\s*\{{[^}}]*--mat-art:", text), f"{family} без кадру"


def test_art_files_exist():
    for family in FAMILIES:
        art = ROOT / "app" / "static" / "img" / f"{family.replace('mat-', 'mat-')}.jpg"
        assert art.exists(), f"немає {art.name}"
        assert art.stat().st_size > 0


def test_consumers_go_through_the_variable():
    """Ні мітка, ні чіп не мають власного url() на кадр матеріалу."""
    for name, selector in (("settings.css", ".matlib-thumb"), ("v2a_passport.css", ".matchip-thumb")):
        text = _css(name)
        assert "var(--mat-art)" in text, f"{name}: {selector} не бере кадр зі змінної"
        assert not re.search(r"url\(\"\.\./img/mat-\w+\.jpg\"\)", text), \
            f"{name}: кадр прописаний напряму — розійдеться з палітрою"


def test_family_class_matches_the_badge_source():
    """Клас родини для бібліотеки береться з того самого каталогу, що й символ
    маркування в черзі — інакше фото й символ показували б різні родини."""
    assert material_family_class("Цирконій") == "mat-zr"
    assert material_family_class("Віск") == "mat-wax"
    # «Не матеріал» — не родина: кадру не має бути.
    assert material_family_class("Не матеріал") == ""
    assert material_family_class(None) == ""
