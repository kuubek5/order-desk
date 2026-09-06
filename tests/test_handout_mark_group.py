"""Кнопка «Усі знайдено» на картці клієнта (аудит 05.09.26, крок 3.5).

У клієнта буває 12-50 робіт, і коли оператор зібрав увесь лоток, він ставив
стільки ж галочок поспіль. Кнопка робить це одним кліком.

Головне, що тут стережеться, — те, чого кнопка НЕ робить. «Знайдено» і
«видано» лишаються двома рішеннями («тримаю коронку» / «віддав логісту»),
бо часткова видача — норма процесу (CLAUDE.md §2): злиття в один клік видало
б роботи, які ще в печі. Тому нижче стоїть тест на те, що статус «видано»
після цієї кнопки не з'являється в жодної роботи.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.sync_control as sync_control
from app.business_day import business_today
from app.db import Base
from app.models import Order, StatusEvent, User
from app.routers import handout as handout_router_mod
from app.services import sheet_writeback as writeback_service
from app.services.handout import MARK_GROUP_DONE, MARK_GROUP_EMPTY, mark_group_found

YESTERDAY = (business_today() - timedelta(days=1)).strftime("%d.%m.%y")
BEFORE = (business_today() - timedelta(days=3)).strftime("%d.%m.%y")


@pytest.fixture(autouse=True)
def _resume_sync():
    sync_control.resume()
    yield
    sync_control.resume()


def _database():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine


def _user(db):
    user = User(username="root", password_hash="unused", full_name="Роман", role="адмін")
    db.add(user)
    db.commit()
    return user


def _order(client_name="Basarab", status="відфрезеровано", row_number=60, sheet_tab=YESTERDAY):
    return Order(
        source="sheet_client", sheet_tab=sheet_tab, row_number=row_number,
        client_name=client_name, material_color="Ti", quantity="1", status=status,
    )


def _request(user_id, headers=None):
    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=headers or {},
    )


class TestService:
    def test_marks_every_pending_work_of_the_client(self):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add_all([_order(row_number=n) for n in (60, 61, 62)])
            db.commit()

            result = mark_group_found(db, user, client_name="Basarab", day="")

            assert result.outcome == MARK_GROUP_DONE
            assert result.count == 3
            assert {o.status for o in db.scalars(select(Order))} == {"знайдено при видачі"}

    def test_it_never_issues(self):
        """Найважливіше в цьому кроці: кнопка не закриває клієнта."""
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add_all([_order(row_number=60), _order(row_number=61)])
            db.commit()

            mark_group_found(db, user, client_name="Basarab", day="")

            assert "видано" not in {o.status for o in db.scalars(select(Order))}

    def test_already_found_work_does_not_get_a_second_event(self):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(row_number=60, status="знайдено при видачі"))
            db.add(_order(row_number=61))
            db.commit()
            already_found = db.scalar(
                select(Order).where(Order.row_number == 60)
            )

            result = mark_group_found(db, user, client_name="Basarab", day="")

            assert result.count == 1
            assert db.scalar(
                select(StatusEvent).where(StatusEvent.order_id == already_found.id)
            ) is None

    def test_nothing_left_to_mark_is_an_empty_outcome(self):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(row_number=60, status="знайдено при видачі"))
            db.commit()

            result = mark_group_found(db, user, client_name="Basarab", day="")
            assert result.outcome == MARK_GROUP_EMPTY

    def test_issued_works_are_left_alone(self):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(row_number=60, status="видано"))
            db.commit()

            result = mark_group_found(db, user, client_name="Basarab", day="")
            assert result.outcome == MARK_GROUP_EMPTY
            assert db.scalar(select(Order)).status == "видано"

    def test_other_clients_are_not_touched(self):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(client_name="Basarab", row_number=60))
            db.add(_order(client_name="Дента", row_number=61))
            db.commit()

            mark_group_found(db, user, client_name="Basarab", day="")

            other = db.scalar(select(Order).where(Order.client_name == "Дента"))
            assert other.status == "відфрезеровано"

    def test_the_day_filter_narrows_the_group(self):
        """Коли екран відфільтровано днем, картка показує роботи цього дня —
        кнопка мусить закривати рівно те, що оператор бачить."""
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(row_number=60, sheet_tab=YESTERDAY))
            db.add(_order(row_number=61, sheet_tab=BEFORE))
            db.commit()

            result = mark_group_found(db, user, client_name="Basarab", day=YESTERDAY)

            assert result.count == 1
            older = db.scalar(select(Order).where(Order.sheet_tab == BEFORE))
            assert older.status == "відфрезеровано"

    def test_todays_work_is_part_of_the_group(self):
        """Сьогоднішній день на видачі ТЕЖ є (ef053c9, 31.08.26: ПММА/титан
        готові в день фрезерування). Групова дія мусить бачити ті самі роботи,
        що й екран — інакше «Усі знайдено» мовчки пропускає сьогоднішні."""
        engine = _database()
        today = business_today().strftime("%d.%m.%y")
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(row_number=60, sheet_tab=today))
            db.commit()

            result = mark_group_found(db, user, client_name="Basarab", day="")
            assert result.count == 1
            assert db.scalar(select(Order).where(Order.row_number == 60)).status == "знайдено при видачі"

    def test_tomorrows_work_is_not_on_the_handout_yet(self):
        """Завтрашнє — ще ні: межа `<= today`, не «все підряд»."""
        engine = _database()
        tomorrow = (business_today() + timedelta(days=1)).strftime("%d.%m.%y")
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(row_number=61, sheet_tab=tomorrow))
            db.commit()

            result = mark_group_found(db, user, client_name="Basarab", day="")
            assert result.outcome == MARK_GROUP_EMPTY


class TestRoute:
    def test_requires_authentication(self):
        engine = _database()
        with Session(engine) as db, pytest.raises(HTTPException) as exc:
            asyncio.run(handout_router_mod.mark_found_group(
                request=_request(None), client_name="X", db=db,
            ))
        assert exc.value.status_code == 401

    def _run(self, db, user, monkeypatch):
        calls = []
        monkeypatch.setattr(
            handout_router_mod, "clear_group_fills_background",
            lambda ids: calls.append(list(ids)),
        )
        asyncio.run(handout_router_mod.mark_found_group(
            request=_request(user.id), client_name="Basarab",
            source="all", day="", db=db,
        ))
        return calls

    def test_the_fill_is_cleared_for_the_whole_group_at_once(self, monkeypatch):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add_all([_order(row_number=60), _order(row_number=61)])
            db.commit()

            calls = self._run(db, user, monkeypatch)

            assert len(calls) == 1, "заливка мусить іти однією пакетною правкою, не по роботі"
            assert len(calls[0]) == 2

    def test_paused_sync_still_marks_but_writes_nothing(self, monkeypatch):
        """Пауза означає «жодного запису в таблицю», але рішення оператора
        в базі лишається — той самий контракт, що в поодинокої галочки."""
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_order(row_number=60))
            db.commit()
            sync_control.pause()

            calls = self._run(db, user, monkeypatch)

            assert calls == []
            assert db.scalar(select(Order)).status == "знайдено при видачі"


class TestBatchedFillWrite:
    """Сама пакетна правка: одне відкриття таблиці на всю групу."""

    def test_one_open_and_one_clear_for_the_whole_group(self, monkeypatch):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            _user(db)
            db.add_all([_order(row_number=n) for n in (60, 61, 62)])
            db.commit()
            ids = [o.id for o in db.scalars(select(Order))]

        opens = []
        cleared = []
        monkeypatch.setattr(writeback_service, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr(
            writeback_service, "open_spreadsheet",
            lambda db=None: opens.append(1) or object(),
        )
        monkeypatch.setattr(
            writeback_service, "get_worksheet_by_name",
            lambda ss, name: SimpleNamespace(id=7, title=name),
        )
        monkeypatch.setattr(
            writeback_service, "resolve_order_row", lambda ws, order: order.row_number + 6,
        )
        monkeypatch.setattr(
            writeback_service, "clear_row_fills",
            lambda ss, rows: cleared.append(list(rows)),
        )

        writeback_service.clear_group_fills_background(ids)
        # Пул — один теплий потік із чергою FIFO, тож порожнє завдання після
        # нашого гарантує, що воно вже відпрацювало.
        writeback_service.submit_sheet_write(lambda: None).result(timeout=10)

        assert opens == [1], "таблиця мусить відкриватись один раз на групу"
        assert cleared == [[(7, 66), (7, 67), (7, 68)]]

    def test_an_empty_group_does_not_touch_the_sheet(self, monkeypatch):
        opens = []
        monkeypatch.setattr(
            writeback_service, "open_spreadsheet", lambda db=None: opens.append(1),
        )

        writeback_service.clear_group_fills_background([])
        writeback_service.submit_sheet_write(lambda: None).result(timeout=10)

        assert opens == []
