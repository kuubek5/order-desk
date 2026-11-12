"""Звірка диска програми з замовленням, теки програм і старі диски (12.11.26).

Що тут стережеться найперше — вимога власника «щоб випадково нічого зайвого не
видалити»: модулі, що дивляться в теки верстатів і теку дисків CAM, не мають
ЖОДНОЇ операції запису. Тест читає їхній код і падає на першому `unlink`,
`rename`, `mkdir`, `write_*`, `shutil.*` чи `open(..., "w")`.
"""

from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.services import disc_check, program_folders
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import ADMIN, OPERATOR, app_db  # noqa: F401 — фікстура

READ_ONLY_MODULES = (
    "app/services/program_folders.py",
    "app/services/disc_check.py",
    "app/services/disc_inventory.py",
    "app/services/shop_history.py",
)
_FORBIDDEN_CALLS = {
    "unlink", "rename", "replace", "rmdir", "remove", "removedirs", "rmtree", "mkdir",
    "makedirs", "write_text", "write_bytes", "touch", "move", "copy", "copy2", "copyfile",
    "copytree", "chmod", "truncate", "symlink_to", "hardlink_to", "link",
}


@pytest.mark.parametrize("module", READ_ONLY_MODULES)
def test_modules_have_no_write_operations(module):
    tree = ast.parse(Path(module).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            if any(n.split(".")[0] in ("shutil", "tempfile") for n in names):
                bad.append(f"import {names}")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        # `str.replace("\\", "/")` — не файл: перший аргумент рядок-літерал.
        # `os.replace(src, dst)` і `Path.replace(target)` так не викликають.
        text_replace = (name == "replace" and node.args
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str))
        if name in _FORBIDDEN_CALLS and not text_replace:
            bad.append(f"{name}() у рядку {node.lineno}")
        if name == "open":
            modes = [a for a in node.args[1:2]] + [k.value for k in node.keywords if k.arg == "mode"]
            for mode in modes:
                if isinstance(mode, ast.Constant) and set(str(mode.value)) & set("wax+"):
                    bad.append(f"open(…, {mode.value!r}) у рядку {node.lineno}")
    assert not bad, f"{module} мусить лише читати: {bad}"


# ── Назва програми → диск ────────────────────────────────────────────────────


@pytest.mark.parametrize("name, family, height, shades, sum3d", [
    ("PMMA14_14-A2-X132_2026-11-12_01-20-12.ISO", "ПММА", 14, {"a2"}, "01-20-12"),
    ("4pmma25_25-a2-x276_2026-11-01_12-01-45.iso", "ПММА", 25, {"a2"}, "12-01-45"),
    ("13PMMA18_18-A1-X138_2026-11-04_11-59-01.iso", "ПММА", 18, {"a1"}, "11-59-01"),
    ("1_14-MONOLITH-A3-X89_2026-10-12_10-00-00.iso", "Цирконій", 14, {"a3"}, "10-00-00"),
    ("zr20_20-Monolith-a3-5-x24_2026-11-01_12-01-45.iso", "Цирконій", 20, {"a3.5"}, "12-01-45"),
    ("zr25_25-Z-Nature--a3-5-x01_2026-11-04_11-59-01.iso", "Цирконій", 25, {"a3.5"}, "11-59-01"),
    ("wax18_18-x33_2026-11-04_11-59-01.iso", "Віск", 18, set(), "11-59-01"),
])
def test_program_name_gives_the_disc(name, family, height, shades, sum3d):
    disc = disc_check.program_disc(name)
    assert disc is not None
    assert (disc.family, disc.height, set(disc.shades), disc.sum3d_id) == (family, height, shades, sum3d)


@pytest.mark.parametrize("name", ["2026-11-12_01-20-12.iso", "DDD.ISO", "", None])
def test_program_without_disc_in_the_name_is_none(name):
    assert disc_check.program_disc(name) is None


# ── Порівняння ───────────────────────────────────────────────────────────────


class _Order:
    def __init__(self, material_color, height=None, family=None):
        self.material_color = material_color
        self.stl_height_mm = height
        self.material = type("M", (), {"name": family})() if family else None


def _disc(name):
    disc = disc_check.program_disc(name)
    assert disc is not None
    return disc


def test_same_material_and_shade_is_a_match():
    v = disc_check.compare(_disc("PMMA14_14-A2-X132_2026-11-12_01-20-12.iso"), _Order("пмма A2"))
    assert v.issues == []


def test_shade_a3_is_not_a3_5():
    v = disc_check.compare(_disc("zr20_20-Monolith-A3-x24_2026-11-01_12-01-45.iso"), _Order("моно А3.5"))
    assert any(i.startswith("колір") for i in v.issues)


def test_zirconia_order_on_pmma_disc_is_flagged():
    v = disc_check.compare(_disc("PMMA14_14-A2-X132_2026-11-12_01-20-12.iso"), _Order("моно A2"))
    assert any(i.startswith("матеріал") for i in v.issues)


def test_doceram_code_800_is_a2():
    v = disc_check.compare(_disc("zr14_14-Doceram-A2-x5_2026-11-12_01-20-12.iso"), _Order("800"))
    assert not any(i.startswith("колір") for i in v.issues)


def test_disc_without_shade_is_skipped_not_flagged():
    v = disc_check.compare(_disc("PMMA20_20-PROZRACH-873_2026-11-12_00-44-27.ISO"), _Order("пмма A2"))
    assert v.issues == [] and "колір" in v.skipped


def test_work_taller_than_disc_is_flagged_and_low_work_is_only_a_note():
    tall = disc_check.compare(_disc("zr14_14-Monolith-A2-x5_2026-11-12_01-20-12.iso"),
                              _Order("моно A2", height=12.5, family="Цирконій"))
    assert any(i.startswith("висота") for i in tall.issues), "12.5 × 1.2 = 15 мм на диску 14"
    low = disc_check.compare(_disc("zr25_25-Monolith-A2-x5_2026-11-12_01-20-12.iso"),
                             _Order("моно A2", height=8.0, family="Цирконій"))
    assert low.issues == [] and low.notes


# ── Звʼязок програми з роботами й прохід заднім числом ──────────────────────


def test_orders_link_by_sum3d_and_date(app_db):  # noqa: F811
    from app.models import Machine, Order

    _app, factory = app_db
    with factory() as db:
        db.add(Machine(name="250i-Sec", host="10.0.0.58", port=8765, created_at=datetime(2026, 11, 1)))
        db.add_all([
            Order(source="sheet_client", sheet_tab="11.11.26", row_number=3, quantity="2",
                  material_color="моно А3.5", sum3d_id="01-20-12", client_name="Клієнт А"),
            Order(source="sheet_client", sheet_tab="11.11.26", row_number=4, quantity="1",
                  material_color="моно А3", sum3d_id="01-20-12", client_name="Клієнт Б"),
            # Той самий час Sum3D місяць тому — чужа робота, не звʼязується.
            Order(source="sheet_client", sheet_tab="01.10.26", row_number=5, quantity="1",
                  material_color="пмма A1", sum3d_id="01-20-12", client_name="Старий"),
        ])
        db.commit()
        # Дата береться з назви програми, не з годинника: тест не залежить від дня.
        disc = disc_check.program_disc("1_14-MONOLITH-A3-X89_2026-11-12_01-20-12.iso")
        names = sorted(o.client_name for o in disc_check.orders_for(db, disc))
        assert names == ["Клієнт А", "Клієнт Б"]


def test_retro_report_on_seeded_day(app_db):  # noqa: F811
    from app.models import Machine, MachineReading, Order

    _app, factory = app_db
    with factory() as db:
        db.add(Machine(name="250i-Sec", host="10.0.0.58", port=8765, created_at=datetime(2026, 11, 1)))
        db.add_all([
            Order(source="sheet_client", sheet_tab="12.11.26", row_number=3, quantity="2",
                  material_color="моно А3.5", sum3d_id="01-20-12", client_name="Клієнт А"),
            Order(source="sheet_client", sheet_tab="12.11.26", row_number=4, quantity="1",
                  material_color="моно А3", sum3d_id="01-20-12", client_name="Клієнт Б"),
        ])
        db.add(MachineReading(host="10.0.0.58", captured_at=datetime.now() - timedelta(hours=1),
                              iso_name="1_14-MONOLITH-A3-X89_2026-11-12_01-20-12.iso",
                              sum3d_id="01-20-12"))
        db.add(MachineReading(host="10.0.0.58", captured_at=datetime.now() - timedelta(hours=2),
                              iso_name="2026-11-12_00-10-00.iso", sum3d_id="00-10-00"))
        db.commit()
        report = disc_check.retro(db, 3)
    assert report["програм"] == 2
    assert report["без_диска_в_назві"] == 1
    assert report["звірено_робіт"] == 2
    assert report["розбіжність"] == 1 and report["збіг"] == 1
    assert report["розбіжності_за_видом"] == {"колір": 1}
    assert report["приклади_розбіжностей"][0]["клієнт"] == "Клієнт А"
    assert report["приклади_розбіжностей"][0]["верстат"] == "250i-Sec"


# ── Тека програм ─────────────────────────────────────────────────────────────


def test_scan_lists_programs_and_changes_nothing(tmp_path):
    (tmp_path / "sub").mkdir()
    a = tmp_path / "PMMA14_14-A2-X132_2026-11-12_01-20-12.ISO"
    b = tmp_path / "sub" / "zr14_14-Monolith-A2-x5_2026-11-12_02-00-00.iso"
    c = tmp_path / "readme.txt"
    for p in (a, b, c):
        p.write_bytes(b"x" * 10)
    os.utime(a, (1_700_000_000, 1_700_000_000))
    before = sorted((p.name, p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))

    scanned = program_folders.scan(str(tmp_path))
    assert scanned.ok
    assert {f.name for f in scanned.files} == {a.name, b.name}
    assert scanned.other_files == 1
    assert scanned.newest(1)[0].name == b.name
    assert "програм: 2" in scanned.summary()
    assert sorted((p.name, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")) == before


def test_scan_of_missing_or_empty_path(tmp_path):
    assert program_folders.scan("").summary() == "шлях не задано"
    assert program_folders.scan(str(tmp_path / "нема")).summary() == "теки не знайдено"
    assert program_folders.scan(str(tmp_path)).summary().startswith("програм немає")


# ── Налаштування: лише адмін, перевірка не зберігає ─────────────────────────


def test_admin_saves_and_checks_program_folder_operator_cannot(app_db, tmp_path):  # noqa: F811
    from app.models import Machine

    app, factory = app_db
    with factory() as db:
        m = Machine(name="250i-Sec", host="10.0.0.58", port=8765, created_at=datetime(2026, 11, 1))
        db.add(m)
        db.commit()
        mid = m.id
    (tmp_path / "PMMA14_14-A2-X132_2026-11-12_01-20-12.ISO").write_bytes(b"x")

    admin = MiniClient(app)
    admin.login(*ADMIN)
    status, _, html = admin.get("/settings")
    assert status == 200 and "Теки програм фрезерування" in html

    status, _, body = admin.post(f"/settings/machines/{mid}/program-folder/check", {"path": str(tmp_path)})
    assert status == 200 and "програм: 1" in body and "pf-ok" in body
    with factory() as db:
        assert db.get(Machine, mid).program_folder == "", "перевірка не зберігає"

    assert admin.post(f"/settings/machines/{mid}/program-folder", {"path": f'"{tmp_path}"'})[0] == 303
    with factory() as db:
        assert db.get(Machine, mid).program_folder == str(tmp_path)

    operator = MiniClient(app)
    operator.login(*OPERATOR)
    assert "Теки програм фрезерування" not in operator.get("/settings")[2]
    assert operator.post(f"/settings/machines/{mid}/program-folder", {"path": ""})[0] == 403
    with factory() as db:
        assert db.get(Machine, mid).program_folder == str(tmp_path)
