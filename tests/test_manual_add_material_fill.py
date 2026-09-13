"""Колір клітинки «Колір роботи» (D) за родиною матеріалу.

У таблиці рядок клієнта залитий синім — і саме синій, а не статус у базі, є
сигналом видачі для всієї лабораторії (CLAUDE.md §2). Матеріал позначають
кольором ОДНІЄЇ клітинки поверх цього синього: ПММА (зокрема каппа) —
помаранчева, титан — зелена, цирконій (моно/емо/800) лишається синім.

Тому два інваріанти: заливка вузька (тільки D) і накладається ПІСЛЯ синього,
інакше синій A:K її затирає.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order, User
from app.services.manual_add import create_manual_batch
from app.sheet_writer import (
    COL_MATERIAL_COLOR,
    _CYAN,
    _GREEN,
    _ORANGE,
    _grid_write_requests,
)


def _fills(requests):
    """Лише заливки: (початковий стовпець, кінцевий стовпець, колір)."""
    out = []
    for r in requests:
        rc = r.get("repeatCell")
        if not rc:
            continue
        rng = rc["range"]
        out.append((
            rng.get("startColumnIndex"), rng.get("endColumnIndex"),
            rc["cell"]["userEnteredFormat"]["backgroundColor"],
        ))
    return out


def _work(material, family):
    return {"quantity": "1", "material_color": material, "e_value": "Клієнт",
            "material_family": family}


def test_pmma_cell_is_orange_and_only_the_material_cell():
    req = _grid_write_requests(
        1, [60], [_work("pmma a2", "ПММА")], paint_blue=True, first=60, last=60)
    fills = _fills(req)
    assert (COL_MATERIAL_COLOR - 1, COL_MATERIAL_COLOR, _ORANGE) in fills
    # вузька: рівно одна колонка (D)
    assert COL_MATERIAL_COLOR - (COL_MATERIAL_COLOR - 1) == 1


def test_titanium_cell_is_green():
    req = _grid_write_requests(
        1, [60], [_work("tit", "Титан")], paint_blue=True, first=60, last=60)
    assert (COL_MATERIAL_COLOR - 1, COL_MATERIAL_COLOR, _GREEN) in _fills(req)


def test_zirconia_keeps_the_blue_row_untouched():
    """Цирконій — більшість рядків; своєї заливки не має, синій просвічує."""
    req = _grid_write_requests(
        1, [60], [_work("mono a3", "Цирконій")], paint_blue=True, first=60, last=60)
    fills = _fills(req)
    assert len(fills) == 1                      # тільки синій A:K
    assert fills[0][0] == 0                     # від колонки A


def test_material_fill_is_applied_after_the_blue_row():
    """Головний інваріант: синій A:K накриває D, тож колір матеріалу мусить
    лягти ПІСЛЯ нього — інакше його не видно."""
    req = _grid_write_requests(
        1, [60], [_work("pmma a2", "ПММА")], paint_blue=True, first=60, last=60)
    kinds = [(i, r["repeatCell"]["range"].get("startColumnIndex"))
             for i, r in enumerate(req) if "repeatCell" in r]
    blue_at = next(i for i, col in kinds if col == 0)
    mat_at = next(i for i, col in kinds if col == COL_MATERIAL_COLOR - 1)
    assert blue_at < mat_at


def test_wax_cell_is_turquoise():
    req = _grid_write_requests(
        1, [60], [_work("wax", "Віск")], paint_blue=True, first=60, last=60)
    assert (COL_MATERIAL_COLOR - 1, COL_MATERIAL_COLOR, _CYAN) in _fills(req)


def test_unknown_material_gets_no_invented_colour():
    """Родину, про яку домовленості в таблиці немає (СЛМ), не фарбуємо."""
    req = _grid_write_requests(
        1, [60], [_work("slm", "СЛМ")], paint_blue=False, first=60, last=60)
    assert _fills(req) == []


# --- наскрізь: родина визначається класифікатором, не текстом ----------------


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _batch(db, material):
    seen: dict = {}

    def write_rows(day, works, *, paint_blue, placement, target_tab):
        seen["works"] = works
        return "26.08.26", [60]

    user = User(username="op", password_hash="x", full_name="Оп",
                role="оператор", sheet_initial="Р")
    db.add(user)
    db.commit()
    result = create_manual_batch(
        db, user=user, work_type="client", target_tab="",
        client_name=["Клієнт"], work_order_no=[""], kind=[""],
        material_color=[material], quantity=["1"], sum3d_id=[""],
        job_code=[""], technician_name=[""], opak=[""],
        write_rows=write_rows,
    )
    assert result.error is None
    return seen["works"][0], db.get(Order, result.created_ids[0])


def test_kappa_is_recognised_as_pmma_before_the_sheet_write():
    """«kappa» не містить слова «пмма», але в таблиці вона помаранчева —
    родину дає класифікатор з аліасами, а не пошук підрядка."""
    with Session(_database(), expire_on_commit=False) as db:
        work, order = _batch(db, "kappa")
        assert work["material_family"] == "ПММА"
        assert order.material_id == work["material_id"]


def test_mono_is_zirconia_so_the_row_stays_blue():
    with Session(_database(), expire_on_commit=False) as db:
        work, _ = _batch(db, "mono a3")
        assert work["material_family"] == "Цирконій"
