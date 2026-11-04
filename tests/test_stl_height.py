"""Товщина конструкції з STL (ідея №29, рішення власника 03.11.26).

Бойова звірка — міст 25-26 (наряд 37896): розмах STL по Z 12.015 мм =
`Height` exocad, Sum3D показав 15.02 = ×1.25. Тут — синтетичні STL тієї ж
будови, щоб тест не залежав від файлів цеху.
"""

import struct
from pathlib import Path

import pytest

from app.services import stl_height


def _binary_stl(path: Path, zs: list[float]) -> Path:
    facets = []
    for i in range(0, len(zs), 3):
        a, b, c = (zs + zs[:2])[i:i + 3]
        facets.append(struct.pack("<12fH", 0, 0, 1, 0, 0, a, 1, 0, b, 0, 1, c, 0))
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(facets)) + b"".join(facets))
    return path


def _ascii_stl(path: Path, zs: list[float]) -> Path:
    lines = ["solid t"]
    for i in range(0, len(zs), 3):
        lines += ["facet normal 0 0 1", "outer loop"]
        lines += [f"vertex 0 0 {z}" for z in zs[i:i + 3]]
        lines += ["endloop", "endfacet"]
    path.write_text("\n".join(lines + ["endsolid t"]), encoding="ascii")
    return path


def test_binary_and_ascii_stl_give_the_z_extent(tmp_path):
    zs = [-5.047, 0.0, 6.968, 1.0, 2.0, 3.0]
    assert stl_height.stl_z_extent(_binary_stl(tmp_path / "a.stl", zs)) == pytest.approx(12.015, abs=1e-4)
    assert stl_height.stl_z_extent(_ascii_stl(tmp_path / "b.stl", zs)) == pytest.approx(12.015, abs=1e-6)


def test_unreadable_or_empty_stl_has_no_height(tmp_path):
    (tmp_path / "junk.stl").write_bytes(b"not an stl")
    assert stl_height.stl_z_extent(tmp_path / "junk.stl") is None
    assert stl_height.stl_z_extent(tmp_path / "missing.stl") is None


def test_only_exocad_exports_are_trusted_as_rotated():
    assert stl_height.is_rotated_export("2026-11-02_00004-018-25-26-bridge_slm_cad.stl")
    assert stl_height.is_rotated_export("X-16-15-bitesplint_CAD.STL")
    # Інші CAD осі введення не гарантують — без підказки, не вгадуємо.
    assert not stl_height.is_rotated_export("Crown_36.stl")
    assert not stl_height.is_rotated_export("model_cad.ply")


def test_folder_takes_the_tallest_rotated_stl_and_skips_the_rest(tmp_path):
    _binary_stl(tmp_path / "job-25-26-bridge_cad.stl", [0.0, 12.0, 6.0])
    _binary_stl(tmp_path / "job-11-crown_cad.stl", [0.0, 8.0, 4.0])
    # Не exocad — ігнорується, хоч і вищий.
    _binary_stl(tmp_path / "scan_upper.stl", [0.0, 30.0, 10.0])
    sub = tmp_path / "mono a3"
    sub.mkdir()
    _binary_stl(sub / "job-21-crown_cad.stl", [0.0, 9.0, 1.0])

    result = stl_height.measure_folder(tmp_path)
    assert result.height_mm == pytest.approx(12.0)
    assert result.files == 3
    assert "scan_upper" not in result.signature


def test_implausible_height_is_dropped(tmp_path):
    _binary_stl(tmp_path / "jaw-model_cad.stl", [0.0, 55.0, 1.0])
    assert stl_height.measure_folder(tmp_path).height_mm is None


def test_thickness_uses_the_family_shrink_and_skips_others():
    # Бойова звірка: наряд 37896, Sum3D показав 15.02 при висоті STL 12.015.
    assert stl_height.thickness_mm(12.015, "Цирконій") == pytest.approx(15.02, abs=1e-2)
    assert stl_height.thickness_mm(9.884, "ПММА") == pytest.approx(9.884)
    assert stl_height.thickness_mm(12.0, "Титан") is None
    assert stl_height.thickness_mm(None, "Цирконій") is None
    assert stl_height.chip_text(14.418) == "14,4"


def test_signature_changes_when_files_change(tmp_path):
    a = _binary_stl(tmp_path / "a_cad.stl", [0.0, 5.0, 1.0])
    first = stl_height.folder_signature([a])
    _binary_stl(tmp_path / "a_cad.stl", [0.0, 5.0, 1.0, 2.0, 3.0, 4.0])
    assert stl_height.folder_signature([a]) != first


# ── Прохід по роботах ────────────────────────────────────────────────────────

from datetime import datetime  # noqa: E402

from sqlalchemy.orm import Session  # noqa: E402

from app.material_catalog import ensure_seeded, material_id_by_name  # noqa: E402
from app.models import Order  # noqa: E402
from conftest import make_memory_engine  # noqa: E402


def _order(db, mats, family, **fields):
    values = {"source": "lab", "status": "прораховано", "created_at": datetime.now(), **fields}
    order = Order(material_id=mats[family], **values)
    db.add(order)
    return order


def test_pass_measures_lab_and_mail_works_and_skips_unchanged(tmp_path):
    tech = tmp_path / "tech"
    export = tmp_path / "export"
    _binary_stl((tech / "2026-11-02_00004-018").mkdir(parents=True) or
                tech / "2026-11-02_00004-018" / "2026-11-02_00004-018-25-26-bridge_slm_cad.stl",
                [-5.047, 6.968, 0.0])
    mail_dir = export / "Неда" / "03.11.26" / "pmma a2"
    mail_dir.mkdir(parents=True)
    _binary_stl(mail_dir / "x-16-15-bitesplint_cad.stl", [0.0, 9.884, 1.0])

    engine = make_memory_engine()
    with Session(engine, expire_on_commit=False) as db:
        ensure_seeded(db)
        mats = material_id_by_name(db)
        lab = _order(db, mats, "Цирконій", job_code="2026-11-02_00004-018")
        mail = _order(db, mats, "ПММА", source="email",
                      export_folder_path="Неда/03.11.26/pmma a2")
        titan = _order(db, mats, "Титан", job_code="2026-11-02_00004-018")
        db.commit()

        # Титан у тій самій теці — мірку не дає нікому (найвищий STL може бути чужий).
        assert stl_height.measure_pending(db, str(tech), export) == 2  # лаба (без висоти) + пошта
        assert lab.stl_height_mm is None and titan.stl_height_sig is None
        assert mail.stl_height_mm == pytest.approx(9.884, abs=1e-3)
        assert stl_height.chip_for(mail)["text"] == "9,9"

        db.delete(titan)
        db.commit()
        assert stl_height.measure_pending(db, str(tech), export) == 1
        assert lab.stl_height_mm == pytest.approx(12.015, abs=1e-3)
        chip = stl_height.chip_for(lab)
        assert chip["text"] == "15,0"
        assert "× 1.25" in chip["title"]

        # Ті самі файли — повторно не міряємо.
        assert stl_height.measure_pending(db, str(tech), export) == 0


def test_pass_skips_issued_and_other_families(tmp_path):
    tech = tmp_path / "tech"
    folder = tech / "J1"
    folder.mkdir(parents=True)
    _binary_stl(folder / "J1-11-crown_cad.stl", [0.0, 8.0, 1.0])
    engine = make_memory_engine()
    with Session(engine, expire_on_commit=False) as db:
        ensure_seeded(db)
        mats = material_id_by_name(db)
        issued = _order(db, mats, "Цирконій", job_code="J1", status="видано")
        db.commit()
        assert stl_height.measure_pending(db, str(tech), None) == 0
        assert issued.stl_height_sig is None
