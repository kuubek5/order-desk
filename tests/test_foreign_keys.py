"""Перевірка зовнішніх ключів у SQLite (K.3).

SQLite за замовчуванням FK не перевіряє: осиротілий рядок (коментар роботи,
якої вже немає) лягає мовчки й спливає через тижні як «робота без наряду» або
як помилка рендера. Вмикаємо перевірку — але ціна помилки несиметрична, тож
на базі, де висячі посилання ВЖЕ є, перевірка вимикається: інакше кожен
наступний запис падав би IntegrityError посеред робочого дня.

Тут перевіряється саме це рішення й те, що живі потоки видалення під
увімкненою перевіркою не ламаються.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import db as db_module
from app.db import Base
from app.models import Attachment, Comment, EmailMessage, Order, StatusEvent


@pytest.fixture(autouse=True)
def _restore_enforcement():
    was = db_module.foreign_keys_enforced()
    yield
    db_module.set_foreign_key_enforcement(was)


def _fk_engine(path: Path):
    """Движок з увімкненою перевіркою — як у застосунку після старту."""
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _on(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return engine


def test_orphan_row_is_refused_when_enforcement_is_on(tmp_path):
    engine = _fk_engine(tmp_path / "k.db")
    with Session(engine) as db:
        db.add(Comment(order_id=999, source="sheet", text="сирота"))
        with pytest.raises(Exception) as exc:
            db.commit()
    assert "FOREIGN KEY" in str(exc.value).upper()


def test_deleting_a_work_takes_its_history_with_it(tmp_path):
    """Найчастіший живий потік: видалення роботи разом із історією. Каскади
    оголошені в ORM, і під увімкненою перевіркою вони мусять і далі
    працювати — інакше «Видалити з черги» падало б у оператора."""
    engine = _fk_engine(tmp_path / "k.db")
    with Session(engine) as db:
        order = Order(source="lab", sheet_tab="07.09.26", row_number=7,
                      work_order_no="24122", status="нове")
        db.add(order)
        db.flush()
        db.add(StatusEvent(order_id=order.id, status="нове", actor="sync"))
        db.add(Comment(order_id=order.id, source="sheet", text="на швидку"))
        db.commit()

        db.delete(order)
        db.commit()

        assert db.scalars(select(StatusEvent)).all() == []
        assert db.scalars(select(Comment)).all() == []


def test_unaccepting_a_letter_does_not_break_on_enforcement(tmp_path):
    """Відкат прийняття видаляє роботу, на яку ще посилаються лист і його
    вкладення. Порядок має бути «відчепити → видалити», інакше під
    увімкненою перевіркою відкат падав би."""
    engine = _fk_engine(tmp_path / "k.db")
    with Session(engine) as db:
        order = Order(source="email", sheet_tab="07.09.26", row_number=8,
                      client_name="Люмі", status="нове")
        db.add(order)
        db.flush()
        email = EmailMessage(uid="u1", status="прийнято", order_id=order.id)
        db.add(email)
        db.flush()
        db.add(Attachment(email_message_id=email.id, order_id=order.id,
                          filename="c.stl", saved_path="/spool/u1/c.stl"))
        db.commit()

        # Саме той порядок, який тримає роут: спершу відчепити, потім видалити.
        for attachment in email.attachments:
            attachment.order_id = None
        email.order_id = None
        db.flush()
        db.delete(order)
        db.commit()

        assert db.scalar(select(EmailMessage)).order_id is None
        assert db.scalars(select(Order)).all() == []


def test_dirty_database_turns_enforcement_off_instead_of_breaking(tmp_path, caplog):
    """Головне рішення блоку: на брудній базі перевірку ВИМИКАЄМО й голосно
    пишемо про це — робочий день дорожчий за принцип."""
    from app.schema import _decide_foreign_key_enforcement

    path = tmp_path / "dirty.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with sqlite3.connect(str(path)) as raw:
        raw.execute(
            "INSERT INTO comments (order_id, source, text) VALUES (999, 'sheet', 'сирота')"
        )

    db_module.set_foreign_key_enforcement(True)
    _decide_foreign_key_enforcement(path)

    assert db_module.foreign_keys_enforced() is False


def test_clean_database_turns_enforcement_on(tmp_path):
    from app.schema import _decide_foreign_key_enforcement

    path = tmp_path / "clean.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Order(source="lab", sheet_tab="07.09.26", row_number=1,
                     work_order_no="24122", status="нове", created_at=datetime.now()))
        db.commit()

    db_module.set_foreign_key_enforcement(False)
    _decide_foreign_key_enforcement(path)

    assert db_module.foreign_keys_enforced() is True
