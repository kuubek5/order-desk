"""Правила підрахунку «Виробітку» — відкалібровані проти реальної відомості.

Ловить саме те, на чому легко помилитись у грошах: джерело→колонка,
виключення переробок і архівних, пріоритет правки над авто, і математику
підков (одиниці входять у цирконій, оплата вдвічі).
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Material, Order, VyrobitokCell
from app.services.vyrobitok import compute_month, save_month_settings, set_cell


AUG = "%d.08.26"


def _db() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _materials(db: Session) -> dict[str, int]:
    ids = {}
    for name in ("Цирконій", "ПММА", "Віск", "СЛМ", "Титан"):
        m = Material(name=name, is_production=True)
        db.add(m)
        db.flush()
        ids[name] = m.id
    db.commit()
    return ids


def _order(db, *, source, material_id, qty, day=5, mill_count=None, archived=False):
    o = Order(
        source=source,
        sheet_tab=AUG % day,
        row_number=day,
        material_id=material_id,
        quantity=str(qty),
        mill_count=mill_count,
        status="відфрезеровано",
    )
    if archived:
        o.archived_at = date(2026, 9, 1)
    db.add(o)
    db.commit()
    return o


def _totals(db):
    return compute_month(db, 2026, 8).totals


def test_units_sum_into_source_and_material_columns():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=30)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=12)
    _order(db, source="email", material_id=mat["Цирконій"], qty=50)
    _order(db, source="sheet_client", material_id=mat["Цирконій"], qty=5)
    _order(db, source="lab", material_id=mat["ПММА"], qty=7)
    _order(db, source="lab", material_id=mat["Титан"], qty=3)

    totals = _totals(db)
    assert totals["lab_zr"] == 42
    # email + sheet_client обидва йдуть у «Пошта».
    assert totals["mail_zr"] == 55
    assert totals["lab_pmma"] == 7
    assert totals["lab_ti"] == 3


def test_rework_is_excluded():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=20)
    # «Який раз фрезерується» >= 2 → переробка, у виробіток не входить.
    _order(db, source="lab", material_id=mat["Цирконій"], qty=99, mill_count="2")
    assert _totals(db)["lab_zr"] == 20


def test_archived_is_excluded():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=20)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=99, archived=True)
    assert _totals(db)["lab_zr"] == 20


def test_slm_not_counted_from_orders():
    db = _db()
    mat = _materials(db)
    # СЛМ у чергу (Orders) не потрапляє; навіть якщо Order з матеріалом СЛМ є,
    # табель його НЕ рахує з Orders — число СЛМ приходить лише зі синку (клітинки).
    _order(db, source="lab", material_id=mat["СЛМ"], qty=40)
    assert _totals(db)["lab_slm"] == 0


def _row(*, naryad="", client=False, kind="", mat="", qty=""):
    from types import SimpleNamespace
    return SimpleNamespace(
        work_order_no=naryad, is_client_row=client, kind=kind,
        material_color=mat, quantity=qty,
    )


def test_slm_classifier_lab_file_and_ignored():
    from app.services.vyrobitok import slm_totals_from_rows
    rows = [
        # наряд-body СЛМ — ігнорується (наряд могли завести, роботу не зробити).
        _row(naryad="29203", kind="каркас гвинтова", mat="слм", qty="4"),
        # CADCAM Команда — лаб, к-сть у колонці кольору.
        _row(client=True, kind="CADCAM Команда", mat="4", qty=""),
        # клієнти — файловий СЛМ, к-сть у колонці кількості.
        _row(client=True, kind="CadCam Energy", mat="", qty="4"),
        _row(client=True, kind="Zanoviak", mat="", qty="12"),
        # моделі — не наша робота, не СЛМ.
        _row(client=True, kind="моделі", mat="", qty="5"),
        # звичайна клієнтська фрезерна робота (є і матеріал, і к-сть) — не СЛМ.
        _row(client=True, kind="Basarab", mat="mono a3", qty="2"),
    ]
    lab, mail = slm_totals_from_rows(rows)
    assert lab == 4
    assert mail == 16  # 4 + 12
    # Пастка CADCAM Команда (лаб) ≠ CadCam Energy (клієнт) — не переплутано.


def test_slm_totals_flow_into_tally_and_override_wins():
    db = _db()
    _materials(db)
    from app.services.vyrobitok import store_slm_totals
    # Синк записав СЛМ у клітинки за день.
    store_slm_totals(db, date(2026, 8, 5), lab_units=4, mail_units=115)
    db.commit()
    totals = _totals(db)
    assert totals["lab_slm"] == 4
    assert totals["mail_slm"] == 115

    # Правка оператора б'є авто; повторний запис синку її не чіпає.
    set_cell(db, date(2026, 8, 5), "mail_slm", 120)
    store_slm_totals(db, date(2026, 8, 5), lab_units=4, mail_units=115)
    db.commit()
    grid = compute_month(db, 2026, 8)
    cell = next(r for r in grid.rows if r["dayn"] == 5)["cells"]["mail_slm"]
    assert cell["num"] == 120 and cell["auto"] == 115 and cell["edited"] is True


def test_override_wins_over_auto_and_marks_edited():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=30)

    set_cell(db, date(2026, 8, 5), "lab_zr", 42)
    grid = compute_month(db, 2026, 8)
    row = next(r for r in grid.rows if r["dayn"] == 5)
    cell = row["cells"]["lab_zr"]
    assert cell["num"] == 42
    assert cell["auto"] == 30
    assert cell["edited"] is True
    assert grid.totals["lab_zr"] == 42

    # Стерти правку → повертається авто, мітка знята.
    set_cell(db, date(2026, 8, 5), "lab_zr", None)
    grid = compute_month(db, 2026, 8)
    cell = next(r for r in grid.rows if r["dayn"] == 5)["cells"]["lab_zr"]
    assert cell["num"] == 30 and cell["edited"] is False


def test_manual_columns_have_no_edited_marker():
    db = _db()
    _materials(db)
    set_cell(db, date(2026, 8, 5), "disks", 7)
    grid = compute_month(db, 2026, 8)
    cell = next(r for r in grid.rows if r["dayn"] == 5)["cells"]["disks"]
    assert cell["num"] == 7
    assert cell["edited"] is False  # ручна колонка — не «виправлення CRM»


def test_auto_snapshot_survives_archiving():
    db = _db()
    mat = _materials(db)
    o = _order(db, source="lab", material_id=mat["Цирконій"], qty=30)
    # Перегляд поки день «живий» знімає авто у сховище.
    assert _totals(db)["lab_zr"] == 30
    snap = db.query(VyrobitokCell).filter_by(day=date(2026, 8, 5), col_key="lab_zr").one()
    assert snap.auto_value == 30
    # Робота зникла з живої таблиці (архів) — знімок тримає число.
    o.archived_at = date(2026, 9, 1)
    db.commit()
    assert _totals(db)["lab_zr"] == 30


def test_money_coefficient_and_pidkovy_double_rate():
    db = _db()
    mat = _materials(db)
    # 200 цирконію (лаб 150 + пошта 50), 20 з них — підкови; диски 100.
    _order(db, source="lab", material_id=mat["Цирконій"], qty=150, day=5)
    _order(db, source="email", material_id=mat["Цирконій"], qty=50, day=6)
    set_cell(db, date(2026, 8, 7), "disks", 100)
    set_cell(db, date(2026, 8, 7), "pidkovy", 20)

    m = compute_month(db, 2026, 8).money
    assert m["zr_total"] == 200
    assert m["disks_total"] == 100
    assert m["coefficient"] == pytest.approx(2.0)  # 200 / 100
    types = {t["name"]: t for t in m["types"]}
    # Цирконій без підков = 180; підкови окремо, ставка вдвічі.
    assert types["Цирконій"]["units"] == 180
    assert types["Підкови"]["units"] == 20
    assert types["Підкови"]["rate"] == pytest.approx(types["Цирконій"]["rate"] * 2)
    # Разом одиниць = 200 (підкови всередині цирконію, не додаються зверху).
    assert m["total_units"] == 200


def test_rate_out_of_band_warns_without_guessing():
    db = _db()
    mat = _materials(db)
    # Коефіцієнт 30 (>26,7) → поза довідником, попередження.
    _order(db, source="lab", material_id=mat["Цирконій"], qty=300, day=5)
    set_cell(db, date(2026, 8, 6), "disks", 10)
    grid = compute_month(db, 2026, 8)
    assert grid.money["rate_out_of_band"] is True
    assert grid.warn is not None

    # Оператор задав ставку → попередження зникло, ставка взята з поля.
    save_month_settings(db, 2026, 8, rate_override="0,7")
    grid = compute_month(db, 2026, 8)
    assert grid.money["rate_out_of_band"] is False
    assert grid.money["rate_zn"] == pytest.approx(0.7)


def test_month_settings_divisor_and_kurs():
    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=100, day=5)
    save_month_settings(db, 2026, 8, kurs="50", people_count=4)
    m = compute_month(db, 2026, 8).money
    assert m["people_count"] == 4
    assert m["share"] == pytest.approx(100 / 4)


def test_body_partial_renders():
    """Тіло-партіал рендериться без Jinja-помилок (макрос клітинки, підсумок,
    гроші, опаки) — парсинг шаблону цього не ловить, лише рендер."""
    from app.routers.deps import templates
    from app.services.vyrobitok import HUE, MATERIAL_COLS, OPAK_PEOPLE

    db = _db()
    mat = _materials(db)
    _order(db, source="lab", material_id=mat["Цирконій"], qty=30)
    grid = compute_month(db, 2026, 8)
    html = templates.env.get_template("_vyrobitok_body.html").render(
        grid=grid, material_cols=MATERIAL_COLS, opak_people=OPAK_PEOPLE, hue=HUE
    )
    assert 'id="vyrobitok-body"' in html
    assert "Разом одиниць" in html
