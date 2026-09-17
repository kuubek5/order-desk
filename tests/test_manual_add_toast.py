"""Успішне додавання каже операторові, КУДИ саме лягла робота.

17.09.26 три роботи пішли у ВЧОРАШНЮ вкладку (форма тримала день із першого
завантаження сторінки), і помітили це аж через півдня: екран казав лише
«додано». Ще тричі поспіль додавання лягало в ТОЙ САМИЙ рядок 66, і це теж
було видно лише в журналі. Тому результат партії несе вкладку Й номери рядків,
а роут кладе їх в адресу для тоста.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import User
from app.services.manual_add import create_manual_batch


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db):
    u = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(u)
    db.commit()
    return u


def _write_rows_returning(tab, rows):
    def _write(day, works, *, paint_blue, placement, target_tab):
        return tab, list(rows)
    return _write


def _add(db, user, *, clients, write_rows):
    return create_manual_batch(
        db, user=user, work_type="client", target_tab="17.09.26",
        client_name=list(clients),
        work_order_no=[""] * len(clients), kind=[""] * len(clients),
        material_color=["mono a3"] * len(clients),
        quantity=["1"] * len(clients), sum3d_id=[""] * len(clients),
        job_code=[""] * len(clients), technician_name=[""] * len(clients),
        opak=[""] * len(clients), write_rows=write_rows,
    )


def test_the_result_carries_the_tab_and_the_rows():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        result = _add(db, user, clients=["Басараб"],
                      write_rows=_write_rows_returning("17.09.26", [67]))

    assert result.error is None
    assert result.tab == "17.09.26"
    assert result.sheet_rows == [67], "без номера рядка тост не скаже, куди лягло"


def test_several_works_carry_every_row():
    """Партія з кількох робіт — оператор має бачити ВСІ рядки, а не перший."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        result = _add(db, user, clients=["Басараб", "Неда", "Лагус"],
                      write_rows=_write_rows_returning("17.09.26", [67, 68, 69]))

    assert result.sheet_rows == [67, 68, 69]
    assert len(result.created_ids) == 3


def test_a_repeat_submit_carries_no_rows_so_no_toast():
    """Повтор (F5) у таблицю не пише нічого, тож і хвалитись нема чим —
    порожній `sheet_rows` і є ознакою «тост не показувати»."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        write = _write_rows_returning("17.09.26", [67])
        first = _add(db, user, clients=["Басараб"], write_rows=write)
        again = _add(db, user, clients=["Басараб"], write_rows=write)

    assert first.sheet_rows == [67]
    assert again.duplicate is True
    assert again.sheet_rows == []
