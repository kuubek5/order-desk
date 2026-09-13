"""Літера оператора при ручному додаванні роботи.

Кнопка «Додати в чергу і таблицю»: якщо в рядку заповнено Sum3D ID, це момент
«я це прорахував» — так само, як ввід Sum3D у черзі (routers/orders.py
set_sum3d_id). Тоді в колонку «Прорахував» (М) має лягти літера оператора,
яку він задав у себе (User.sheet_initial), а статус — стати «прораховано».
Без Sum3D або без літери — колишня поведінка.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order, User
from app.services.manual_add import create_manual_batch
from app.sheet_writer import COL_CALCULATED, _row_value_map


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, initial="Р"):
    u = User(username="op", password_hash="x", full_name="Оп",
             role="оператор", sheet_initial=initial)
    db.add(u)
    db.commit()
    return u


def _capture_write_rows(sink):
    """Фейковий запис у таблицю: складає works у sink, віддає (вкладка, рядки)."""
    def _write(day, works, *, paint_blue, placement, target_tab):
        sink["works"] = works
        return "26.08.26", list(range(60, 60 + len(works)))
    return _write


# --- writer level: клітинка М --------------------------------------------------


def test_letter_lands_in_column_M():
    cells = _row_value_map({
        "quantity": "1", "material_color": "мono a3", "e_value": "Басараб",
        "sum3d_id": "12-01-45", "calculated": "Р",
    })
    assert cells[COL_CALCULATED] == "Р"


def test_no_letter_leaves_column_M_alone():
    cells = _row_value_map({
        "quantity": "1", "material_color": "мono a3", "e_value": "Басараб",
        "sum3d_id": "12-01-45",
    })
    assert COL_CALCULATED not in cells


# --- batch level: штамп + статус ----------------------------------------------


def test_manual_add_with_sum3d_stamps_letter_and_advances_status():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        sink: dict = {}
        result = create_manual_batch(
            db, user=user, work_type="client", target_tab="",
            client_name=["Басараб"], work_order_no=[""], kind=[""],
            material_color=["мono a3"], quantity=["1"], sum3d_id=["12-01-45"],
            job_code=[""], technician_name=[""], opak=[""],
            write_rows=_capture_write_rows(sink),
        )
        assert result.error is None
        assert sink["works"][0].get("calculated") == "Р"  # донесли до запису
        order = db.get(Order, result.created_ids[0])
        assert order.calculated_raw == "Р"
        assert order.status == "прораховано"


def test_manual_add_without_sum3d_leaves_operator_empty():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial="Р")
        sink: dict = {}
        result = create_manual_batch(
            db, user=user, work_type="client", target_tab="",
            client_name=["Басараб"], work_order_no=[""], kind=[""],
            material_color=["мono a3"], quantity=["1"], sum3d_id=[""],
            job_code=[""], technician_name=[""], opak=[""],
            write_rows=_capture_write_rows(sink),
        )
        assert result.error is None
        assert "calculated" not in sink["works"][0]
        order = db.get(Order, result.created_ids[0])
        assert order.calculated_raw is None
        assert order.status == "нове"


def test_manual_add_sum3d_but_operator_has_no_letter():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db, initial=None)
        sink: dict = {}
        result = create_manual_batch(
            db, user=user, work_type="client", target_tab="",
            client_name=["Басараб"], work_order_no=[""], kind=[""],
            material_color=["мono a3"], quantity=["1"], sum3d_id=["12-01-45"],
            job_code=[""], technician_name=[""], opak=[""],
            write_rows=_capture_write_rows(sink),
        )
        assert result.error is None
        assert "calculated" not in sink["works"][0]
        order = db.get(Order, result.created_ids[0])
        assert order.calculated_raw is None
        # без літери статус лишається клієнтським «нове» (не чіпаємо)
        assert order.status == "нове"
