"""Товщина з файлів листа в рядку «Вхідних» (власник 05.11.26, варіант B).

Правило те саме, що для теки роботи: найвищий `…_cad.stl`, цирконій × 1.25,
ПММА × 1.0. Роботи ще немає, тож файли беруться з вкладень листа.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import Attachment, EmailMessage
from app.services import stl_height
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


@pytest.fixture(autouse=True)
def _fresh_caches():
    from app.services.material_suggest import invalidate_cache

    invalidate_cache()
    stl_height._extent_cache.clear()
    yield
    invalidate_cache()
    stl_height._extent_cache.clear()


def _stl(path: Path, top: float) -> Path:
    facet = struct.pack("<12fH", 0, 0, 1, 0, 0, 0.0, 1, 0, top, 0, 1, top / 2, 0)
    path.write_bytes(b"\0" * 80 + struct.pack("<I", 1) + facet)
    return path


def _att(path: Path, name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(filename=name or path.name, saved_path=str(path))


def test_tallest_rotated_stl_of_the_letter_wins(tmp_path):
    files = [
        _att(_stl(tmp_path / "a-11-crown_cad.stl", 8.0)),
        _att(_stl(tmp_path / "a-25-26-bridge_cad.stl", 12.0)),
        # Не exocad: осі не гарантує, хоч і вищий.
        _att(_stl(tmp_path / "scan_upper.stl", 20.0)),
        # Модель щелепи: не конструкція.
        _att(_stl(tmp_path / "jaw_cad.stl", 55.0)),
    ]
    assert stl_height.letter_height_mm(files) == pytest.approx(12.0)


def test_family_decides_the_multiplier_and_unknown_gives_no_chip(tmp_path):
    files = [_att(_stl(tmp_path / "a_cad.stl", 12.0))]
    assert stl_height.letter_chip(files, "Zr")["text"] == "15,0"
    assert stl_height.letter_chip(files, "PMMA")["text"] == "12,0"
    assert stl_height.letter_chip(files, "Ti") is None
    assert stl_height.letter_chip(files, None) is None
    assert stl_height.letter_chip([], "Zr") is None


def test_mixed_letter_says_the_number_is_for_one_material(tmp_path):
    files = [_att(_stl(tmp_path / "a_cad.stl", 12.0))]
    assert "кілька матеріалів" in stl_height.letter_chip(files, "PMMA", mixed=True)["title"]
    assert "кілька матеріалів" not in stl_height.letter_chip(files, "PMMA")["title"]


def test_file_is_read_once_until_it_changes(tmp_path, monkeypatch):
    """Список перемальовується раз на 15 с: читати STL щоразу не можна."""
    path = _stl(tmp_path / "a_cad.stl", 12.0)
    real = stl_height.stl_z_extent
    reads: list[Path] = []
    monkeypatch.setattr(stl_height, "stl_z_extent", lambda p: reads.append(p) or real(p))
    files = [_att(path)]

    for _ in range(3):
        assert stl_height.letter_height_mm(files) == pytest.approx(12.0)
    assert len(reads) == 1

    _stl(path, 9.0)
    stamp = path.stat().st_mtime + 120
    os.utime(path, (stamp, stamp))
    assert stl_height.letter_height_mm(files) == pytest.approx(9.0)
    assert len(reads) == 2


def test_broken_or_missing_file_is_quiet_and_not_reread(tmp_path, monkeypatch):
    broken = tmp_path / "broken_cad.stl"
    broken.write_bytes(b"not an stl")
    real = stl_height.stl_z_extent
    reads: list[Path] = []
    monkeypatch.setattr(stl_height, "stl_z_extent", lambda p: reads.append(p) or real(p))
    files = [_att(broken), _att(tmp_path / "gone_cad.stl")]

    assert stl_height.letter_height_mm(files) is None
    assert stl_height.letter_height_mm(files) is None
    assert reads == [broken]


# ── Рядок списку ────────────────────────────────────────────────────────────


def _seed_library(factory) -> None:
    from datetime import timedelta

    from app.business_day import utc_now
    from app.material_catalog import backfill_orders
    from app.models import Order
    from app.services.material_suggest import invalidate_cache

    with factory() as db:
        for text, n in {"mono a3": 9, "pmma a2": 6, "tit": 6}.items():
            for _ in range(n):
                db.add(Order(source="lab", status="нове", material_color=text,
                             created_at=utc_now() - timedelta(days=1)))
        db.flush()
        backfill_orders(db, only_unresolved=False)
        db.commit()
    invalidate_cache()


def _letter(factory, uid: str, stl: Path | None = None, **fields) -> int:
    values = {
        "uid": uid, "uid_validity": "1", "from_address": f"c{uid}@ukr.net",
        "from_name": f"Клієнт {uid}", "subject": "робота", "status": "нове",
        "attachments_status": "ready", "message_id": f"<{uid}@x>",
        "material_color_guess": "mono a3", **fields,
    }
    with factory() as db:
        email = EmailMessage(**values)
        db.add(email)
        db.flush()
        if stl is not None:
            db.add(Attachment(email_message_id=email.id, filename=stl.name, saved_path=str(stl)))
        db.commit()
        return email.id


def _r2(page: str, email_id: int) -> str:
    start = page.index(f'id="mailrow-{email_id}"')
    start = page.index('class="r2"', start)
    return page[start:page.index('<span class="t"', start)]  # у SVG диска теж є class="t"


def test_row_shows_thickness_before_the_files_column(app_db, tmp_path):  # noqa: F811
    app, factory = app_db
    _seed_library(factory)
    zirconia = _letter(factory, "1", _stl(tmp_path / "z_cad.stl", 12.0), subject="Monolith a3")
    pmma = _letter(factory, "2", _stl(tmp_path / "p_cad.stl", 12.0),
                   material_color_guess="pmma a2", subject="pmma a2")
    titan = _letter(factory, "3", _stl(tmp_path / "t_cad.stl", 12.0),
                    material_color_guess="tit", subject="титан")
    other_cad = _letter(factory, "4", _stl(tmp_path / "scan.stl", 12.0), subject="Monolith a3")
    client = MiniClient(app)
    client.login(*OPERATOR)

    _, _, page = client.get("/mail")
    row = _r2(page, zirconia)
    assert "stl-thick" in row and "15,0" in row
    # Колонка стоїть між темою й файлами.
    assert row.index('class="subj"') < row.index("mail-thick-slot") < row.index('class="att')
    assert "12,0" in _r2(page, pmma)
    # Комірка лишається й порожня, щоб стовпчик не стрибав.
    for email_id in (titan, other_cad):
        cell = _r2(page, email_id)
        assert "mail-thick-slot" in cell and "stl-thick" not in cell


@pytest.mark.parametrize("status", ["skipped", "pending"])
def test_files_not_fully_downloaded_give_no_number(app_db, tmp_path, status):  # noqa: F811
    app, factory = app_db
    _seed_library(factory)
    email_id = _letter(factory, "1", _stl(tmp_path / "z_cad.stl", 12.0),
                       subject="Monolith a3", attachments_status=status)
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, page = client.get("/mail")
    assert "stl-thick" not in _r2(page, email_id)


def test_card_has_no_thickness_badge(app_db, tmp_path):  # noqa: F811
    """Власник 05.11.26: досить бейджа в рядку списку."""
    app, factory = app_db
    _seed_library(factory)
    email_id = _letter(factory, "1", _stl(tmp_path / "z_cad.stl", 12.0), subject="Monolith a3")
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, card = client.get(f"/mail/{email_id}?panel=1")
    assert "stl-thick" not in card


def test_other_tabs_have_no_extra_grid_cell(app_db, tmp_path):  # noqa: F811
    """Колонка лише у «Вхідних»: в інших вкладках сітка рядка триколонкова."""
    app, factory = app_db
    _seed_library(factory)
    _letter(factory, "1", _stl(tmp_path / "z_cad.stl", 12.0), subject="Monolith a3", status="відхилено")
    client = MiniClient(app)
    client.login(*OPERATOR)
    _, _, page = client.get("/mail?view=archive")
    assert re.search(r'id="mailrow-\d+"', page)
    assert "mail-thick-slot" not in page


def test_queue_row_still_draws_the_same_badge(tmp_path):
    """Спільний партіал: бейдж черги не мав змінитись від винесення."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
    chip = stl_height.chip_from(12.015, "Цирконій")
    html = env.get_template("_stl_thick.html").render(_thick=chip)
    assert 'class="stl-thick mono"' in html
    assert "15,0" in html
    assert f'cy="{chip["fill_y"]}"' in html
