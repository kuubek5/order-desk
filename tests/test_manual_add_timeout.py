"""«Не дочекались» — це НЕ «не вдалося».

Пул запису — звичайний ThreadPoolExecutor, і `.result(timeout=…)` лише
перестає ЧЕКАТИ: задача, що вже виконується, добігає до кінця. Найгірший
випадок одного звернення до Google — близько 287 с (4 спроби по 10+60 с плюс
паузи), а таких звернень у додаванні чотири; чекаємо ж 120 с. Тобто на поганому
зв'язку «не дочекались» настає РАНІШЕ, ніж запис справді провалився.

Доти обидва випадки зливались в одне «Не вдалося записати в таблицю». Оператор
вірив, набирав заново — і обидва записи лягали: два рядки в таблиці, дві роботи
в черзі після синку, коронка фрезерується двічі.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order, SyncLog, User
from app.services.manual_add import WriteStillRunning, create_manual_batch


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def _user(db):
    """Один оператор на базу: повторне створення впало б на унікальному логіні,
    а тести додають по кілька разів."""
    existing = db.scalars(select(User).where(User.username == "op")).first()
    if existing is not None:
        return existing
    u = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(u)
    db.commit()
    return u


def _add(db, write_rows, *, user=None):
    return create_manual_batch(
        db, user=user or _user(db), work_type="client", target_tab="",
        client_name=["Басараб"], work_order_no=[""], kind=[""],
        material_color=["mono a3"], quantity=["1"], sum3d_id=[""],
        job_code=[""], technician_name=[""], opak=[""],
        write_rows=write_rows,
    )


def _raise(exc):
    def _w(*a, **k):
        raise exc
    return _w


def test_timeout_tells_the_operator_to_check_before_re_adding(db):
    """Слово «зачекайте» тут важливіше за слово «помилка»: помилка штовхає
    повторити, і саме це давало дублі."""
    result = _add(db, _raise(WriteStillRunning()))
    assert result.error is not None
    assert "ЩЕ В ЧЕРЗІ" in result.error
    assert "перевірте таблицю" in result.error
    assert "Не вдалося" not in result.error


def test_a_real_failure_still_says_it_failed(db):
    result = _add(db, _raise(RuntimeError("доступу немає")))
    assert "Не вдалося записати в таблицю" in result.error
    assert "доступу немає" in result.error


def test_neither_case_creates_a_work(db):
    """Робота народжується лише з номерів рядків, які повернув запис — інакше
    CRM показувала б роботу, якої в таблиці немає."""
    for exc in (WriteStillRunning(), RuntimeError("мережа")):
        assert _add(db, _raise(exc)).created_ids == []
    assert db.scalars(select(Order)).all() == []


def test_failure_leaves_a_trace_in_the_journal(db):
    """Доти успіх писав рядок, а невдача — ні: зник тост, і по роботі, якої
    оператор не додав, не лишалось нічого видимого з інтерфейсу."""
    _add(db, _raise(RuntimeError("мережа")))
    rows = db.scalars(select(SyncLog)).all()
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].direction == "db_to_sheet"
    assert "ручне додавання" in rows[0].message


def test_timeout_is_logged_as_still_queued_not_as_a_failure(db):
    _add(db, _raise(WriteStillRunning()))
    row = db.scalars(select(SyncLog)).one()
    assert "не відповіла вчасно" in row.message
    assert "лишився в черзі" in row.message


def test_a_broken_journal_never_blocks_the_answer(db):
    """Журнал — страховка. Якщо він упаде, оператор усе одно мусить отримати
    своє повідомлення, а не 500."""
    # Ламаємо САМЕ журнал, а не сесію: підміна `db.add` завалила б ще й
    # засівання каталогу матеріалів, яке йде до запису, і тест «доводив» би
    # падіння зовсім в іншому місці.
    with patch("app.services.manual_add.SyncLog", side_effect=RuntimeError("база недоступна")):
        result = _add(db, _raise(RuntimeError("мережа")))
    assert "Не вдалося записати в таблицю" in result.error
    assert db.scalars(select(SyncLog)).all() == []   # журнал справді не записався


def test_the_route_turns_a_wait_timeout_into_this_type():
    """Без цього перетворення сервіс не відрізнив би «не дочекались» від
    «впало», і весь сенс правки зник би."""
    from pathlib import Path

    src = Path("app/routers/orders.py").read_text(encoding="utf-8")
    assert "FuturesTimeout" in src
    assert "raise WriteStillRunning()" in src


def test_waiting_does_not_cancel_the_write():
    """Сторож проти «оптимізації», яка колись здасться очевидною: скасувати
    задачу пулу не можна — вона вже виконується, і саме тому текст просить
    ПЕРЕВІРИТИ таблицю."""
    from pathlib import Path

    src = Path("app/routers/orders.py").read_text(encoding="utf-8")
    assert "future.cancel()" not in src
