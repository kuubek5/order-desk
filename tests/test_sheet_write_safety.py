"""Запис у Google Таблицю: НЕ на event loop і НЕ в чужий рядок.

Аудит 05.09.26 (синк C-2 і H-5). Дві незалежні гарантії:

1. Роути `async def` більше не ходять у Google з самого циклу подій. FastAPI
   виконує `async def` без threadpool, тож синхронний gspread усередині
   заморожував ВЕСЬ застосунок: холодне відкриття таблиці ~40 с, сон після 429
   — 20/40/60 с, «Видати 8 з 8» — понад дві хвилини тиші для всіх.
2. Кожен запис звіряє, що рядок досі несе саме цю роботу. Технік видаляє рядок
   у таблиці — Google зсуває все нижче на одиницю, і збережений row_number
   починає показувати на чужу живу роботу.
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

import app.web as web
from app.db import Base
from app.models import Order, User
from app.routers import orders as orders_router_mod
from app.services import sheet_writeback as writeback


def _engine():
    return create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def _user(db):
    user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
    db.add(user)
    db.commit()
    return user


def _order(db, **kw):
    values = dict(source="lab", sheet_tab="26.08.26", row_number=7,
                  work_order_no="24122", status="прийнято")
    values.update(kw)
    order = Order(**values)
    db.add(order)
    db.commit()
    return order


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        headers=Headers({}),
    )


class TestWritesLeaveTheEventLoop:
    def test_sum3d_write_runs_on_the_writeback_worker(self, monkeypatch):
        engine = _engine()
        Base.metadata.create_all(engine)
        monkeypatch.setattr(
            writeback, "writeback_session",
            sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
        )

        threads: list[str] = []

        def spy(db, order, fields):
            threads.append(threading.current_thread().name)
            return None

        monkeypatch.setattr(writeback, "write_sheet_fields", spy)
        monkeypatch.setattr(orders_router_mod, "attach_export_folder_uris", lambda *a: None)
        monkeypatch.setattr(orders_router_mod, "attach_job_code_folder_uris", lambda *a: None)
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context: SimpleNamespace(headers={}),
        )

        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            order = _order(db)
            loop_thread = None

            async def drive():
                nonlocal loop_thread
                loop_thread = threading.current_thread().name
                await orders_router_mod.set_sum3d_id(
                    request=_request(user.id), order_id=order.id,
                    sum3d_id="12-01-45", db=db,
                )

            asyncio.run(drive())

        assert threads, "запис у таблицю не відбувся"
        assert threads[0].startswith("sheet-writeback"), threads
        assert threads[0] != loop_thread

    def test_group_write_is_one_job_on_the_worker(self):
        """Раніше видача групи була циклом із відкриттям таблиці на event loop —
        по ~4 виклики на кожну роботу. Тепер уся група — одне завдання."""
        jobs: list[str] = []

        def spy_group(field_map):
            jobs.append(threading.current_thread().name)
            return None

        assert asyncio.run(
            writeback.await_on_writeback(spy_group, {1: ["milled_raw"]})
        ) is None
        assert len(jobs) == 1
        assert jobs[0].startswith("sheet-writeback")

    def test_a_slow_write_does_not_block_the_loop(self):
        """Поки таблиця відповідає, цикл подій мусить обслуговувати інших."""
        started = threading.Event()
        release = threading.Event()
        ticks = 0

        def slow():
            started.set()
            release.wait(5)
            return None

        async def drive():
            nonlocal ticks
            task = asyncio.ensure_future(writeback.await_on_writeback(slow))
            started.wait(5)
            for _ in range(3):
                await asyncio.sleep(0)
                ticks += 1
            release.set()
            return await task

        assert asyncio.run(drive()) is None
        assert ticks == 3  # цикл жив увесь час запису

    def test_a_failing_write_is_reported_not_raised(self):
        """Оператор мусить бачити «не записано», а не 500 і не тихий успіх."""
        def boom():
            raise RuntimeError("Google відмовив")

        error = asyncio.run(writeback.await_on_writeback(boom))
        assert error == "Google відмовив"


class TestWritesConfirmTheRow:
    def _worksheet(self, cell_value, column_values):
        ws = MagicMock()
        ws.id = 11
        ws.cell.return_value = SimpleNamespace(value=cell_value)
        ws.col_values.return_value = column_values
        return ws

    def _lab_order(self):
        return SimpleNamespace(
            id=1, row_number=7, source="lab", work_order_no="24122",
            client_name=None, sheet_tab="26.08.26",
        )

    def test_resolve_skips_when_the_check_read_fails(self):
        """Повтори вичерпані — звірки НЕ БУЛО. Раніше саме тут запис ішов у
        неперевірений рядок (синк M-11)."""
        from app.sheet_writer import resolve_order_row

        ws = MagicMock()
        ws.cell.side_effect = RuntimeError("проксі обірвав зʼєднання")
        assert resolve_order_row(ws, self._lab_order()) is None

    def test_resolve_relocates_a_shifted_row(self):
        from app.sheet_writer import resolve_order_row

        # Рядок 13 (7 + HEADER_ROWS) тепер несе чужий наряд; наш — рядком вище.
        ws = self._worksheet("99999", [""] * 11 + ["24122", "99999"])
        assert resolve_order_row(ws, self._lab_order()) == 12

    def test_clear_order_row_refuses_an_unconfirmed_row(self, monkeypatch):
        """Стирання чистить A:K — влучання в сусіда знищує чужу живу роботу."""
        import app.sheet_writer as sheet_writer

        cleared: list[int] = []
        monkeypatch.setattr(
            sheet_writer, "clear_placeholder_row",
            lambda worksheet, row: cleared.append(row),
        )
        ws = self._worksheet("99999", ["99999"] * 20)  # не наше / неоднозначно

        assert sheet_writer.clear_order_row(ws, self._lab_order()) is False
        assert cleared == []

    def test_clear_order_row_erases_the_confirmed_row(self, monkeypatch):
        import app.sheet_writer as sheet_writer

        cleared: list[int] = []
        monkeypatch.setattr(
            sheet_writer, "clear_placeholder_row",
            lambda worksheet, row: cleared.append(row),
        )
        ws = self._worksheet("24122", [])
        assert sheet_writer.clear_order_row(ws, self._lab_order()) is True
        assert cleared == [13]

    def test_comment_append_refuses_an_unconfirmed_row(self):
        """Коментар не просто пише — він ДОПИСУЄ, тож у чужій клітинці ще й
        тягне за собою чужий текст."""
        from app.sheet_writer import append_order_comment

        ws = self._worksheet("99999", ["99999"] * 20)
        with pytest.raises(RuntimeError, match="не підтверджено"):
            append_order_comment(ws, self._lab_order(), "нотатка")
        ws.update_cell.assert_not_called()

    def test_client_fill_skips_an_unconfirmed_row(self, monkeypatch):
        """Заливка E — це «видано» в таблиці. Не той рядок = не той клієнт."""
        painted: list = []
        monkeypatch.setattr(writeback, "paint_row_fills",
                            lambda ss, rows: painted.append(rows))
        monkeypatch.setattr(writeback, "clear_row_fills",
                            lambda ss, rows: painted.append(rows))
        monkeypatch.setattr(writeback, "open_spreadsheet", lambda db=None: MagicMock())
        ws = self._worksheet("Інший Клієнт", ["Інший Клієнт"] * 20)
        monkeypatch.setattr(writeback, "get_worksheet_by_name", lambda ss, tab: ws)

        order = SimpleNamespace(
            id=5, row_number=7, source="sheet_client", sheet_tab="26.08.26",
            client_name="Vision", work_order_no=None,
        )
        error = writeback.set_client_row_fill(MagicMock(), order, blue=False)

        assert error and "не підтверджено" in error
        assert painted == []

    def test_client_fill_uses_the_relocated_row(self, monkeypatch):
        painted: list = []
        monkeypatch.setattr(writeback, "clear_row_fills",
                            lambda ss, rows: painted.append(rows))
        monkeypatch.setattr(writeback, "open_spreadsheet", lambda db=None: MagicMock())
        ws = self._worksheet("Хтось Інший", [""] * 11 + ["Vision"])
        monkeypatch.setattr(writeback, "get_worksheet_by_name", lambda ss, tab: ws)

        order = SimpleNamespace(
            id=5, row_number=7, source="sheet_client", sheet_tab="26.08.26",
            client_name="Vision", work_order_no=None,
        )
        assert writeback.set_client_row_fill(MagicMock(), order, blue=False) is None
        assert painted == [[(11, 12)]]  # зсунутий рядок, не збережений 13


class TestEmptyCellIsNotAConfirmation:
    """Ревʼю 07.09.26 (sync CRITICAL-1/2). gspread віддає None для порожньої
    клітинки, і звірка позиції довіряла збереженому рядку — а після видалення
    рядка вище сюди зʼїжджає сусід, у якого наряд часто порожній. Стирання
    A:K тоді знищувало чужу живу роботу."""

    def _worksheet(self, cell_value, column_values):
        ws = MagicMock()
        ws.id = 11
        ws.cell.return_value = SimpleNamespace(value=cell_value)
        ws.col_values.return_value = column_values
        return ws

    def _lab_order(self):
        return SimpleNamespace(
            id=1, row_number=7, source="lab", work_order_no="24122",
            client_name=None, sheet_tab="26.08.26",
        )

    def test_empty_cell_relocates_instead_of_trusting_the_stored_row(self):
        from app.sheet_writer import resolve_order_row

        # На збереженій позиції (13) порожньо; наш наряд тепер рядком вище.
        ws = self._worksheet(None, [""] * 11 + ["24122"])
        assert resolve_order_row(ws, self._lab_order()) == 12

    def test_empty_cell_with_no_match_anywhere_skips(self):
        from app.sheet_writer import resolve_order_row

        ws = self._worksheet(None, ["99999"] * 20)
        assert resolve_order_row(ws, self._lab_order()) is None

    def test_email_order_is_anchored_on_the_client_name(self):
        """Рядок-нотатка поштової роботи має імʼя клієнта в колонці E — це і є
        її якір; раніше source="email" не мав якоря взагалі."""
        from app.sheet_writer import resolve_order_row

        order = SimpleNamespace(
            id=2, row_number=7, source="email", work_order_no=None,
            client_name="Кривовид", sheet_tab="26.08.26",
        )
        ws = self._worksheet("Хтось інший", [""] * 14 + ["Кривовид"])
        assert resolve_order_row(ws, order) == 15

    def test_clear_without_an_anchor_is_refused(self, monkeypatch):
        """Наряд-less лабораторний рядок: писати в нього ще можна (оптимістично),
        стирати A:K — ніколи."""
        import app.sheet_writer as sheet_writer

        cleared: list[int] = []
        monkeypatch.setattr(
            sheet_writer, "clear_placeholder_row",
            lambda worksheet, row: cleared.append(row),
        )
        order = SimpleNamespace(
            id=3, row_number=7, source="lab", work_order_no="",
            client_name=None, sheet_tab="26.08.26",
        )
        ws = self._worksheet("", [])
        assert sheet_writer.clear_order_row(ws, order) is False
        assert cleared == []


class TestErasedRowLeavesATrace:
    """Ревʼю 07.09.26: у спільній таблиці «щось зникло, і невідомо що там було»
    — найгірший результат. Перед стиранням вміст рядка читається й лягає в
    журнал, щоб рядок можна було набрати назад."""

    def _worksheet(self, cell_value, column_values, row_values=None):
        ws = MagicMock()
        ws.id = 11
        ws.cell.return_value = SimpleNamespace(value=cell_value)
        ws.col_values.return_value = column_values
        ws.get_values.return_value = [row_values] if row_values else []
        return ws

    def _lab_order(self):
        return SimpleNamespace(
            id=42, row_number=7, source="lab", work_order_no="24122",
            client_name=None, sheet_tab="26.08.26",
        )

    def test_row_content_is_captured_before_the_wipe(self, monkeypatch):
        import app.sheet_writer as sheet_writer

        monkeypatch.setattr(sheet_writer, "clear_placeholder_row", lambda ws, row: None)
        ws = self._worksheet("24122", [], ["1", "24122", "2", "мono a3", "анатомія"])

        assert sheet_writer.clear_order_row(ws, self._lab_order()) is True
        row_no, values = sheet_writer.take_last_erased(42)
        assert row_no == 13
        assert "24122" in values
        # Забрали — вдруге вже порожньо, щоб старий вміст не приліпився до
        # наступного стирання.
        assert sheet_writer.take_last_erased(42) is None

    def test_unreadable_row_still_gets_erased(self, monkeypatch):
        """Журнал — страховка, а не умова: збій читання не блокує стирання."""
        import app.sheet_writer as sheet_writer

        cleared = []
        monkeypatch.setattr(
            sheet_writer, "clear_placeholder_row", lambda ws, row: cleared.append(row)
        )
        ws = self._worksheet("24122", [])
        ws.get_values.side_effect = RuntimeError("проксі обірвав зʼєднання")

        assert sheet_writer.clear_order_row(ws, self._lab_order()) is True
        assert cleared == [13]
        row_no, values = sheet_writer.take_last_erased(42)
        assert values == []


class TestMassEraseGuard:
    """B.7: звірка позиції захищає ОДНЕ стирання, стеля — від розгону.
    Цикл у коді або повторний відкат стерли б десятки рядків спільної
    таблиці, і кожне стирання окремо виглядало б законним."""

    def _worksheet(self):
        ws = MagicMock()
        ws.id = 11
        ws.cell.return_value = SimpleNamespace(value="24122")
        ws.col_values.return_value = []
        ws.get_values.return_value = []
        return ws

    def _lab_order(self):
        return SimpleNamespace(
            id=42, row_number=7, source="lab", work_order_no="24122",
            client_name=None, sheet_tab="26.08.26",
        )

    def test_erase_is_blocked_once_the_hourly_ceiling_is_reached(self, monkeypatch):
        import app.sheet_writer as sheet_writer
        from app import sheet_erase_guard
        from app.sheet_erase_guard import SheetEraseBlocked

        cleared: list[int] = []
        monkeypatch.setattr(
            sheet_writer, "clear_placeholder_row", lambda ws, row: cleared.append(row)
        )
        ws = self._worksheet()

        for _ in range(sheet_erase_guard.LIMIT):
            assert sheet_writer.clear_order_row(ws, self._lab_order()) is True
        assert len(cleared) == sheet_erase_guard.LIMIT

        with pytest.raises(SheetEraseBlocked):
            sheet_writer.clear_order_row(ws, self._lab_order())
        # Рядок лишається в таблиці — безпечний бік відмови.
        assert len(cleared) == sheet_erase_guard.LIMIT

    def test_skipped_erases_do_not_eat_the_ceiling(self, monkeypatch):
        """Непідтверджений рядок нічого не стирає, тож і стелю не витрачає —
        інакше кілька відмов поспіль заблокували б законне стирання."""
        import app.sheet_writer as sheet_writer
        from app import sheet_erase_guard

        monkeypatch.setattr(sheet_writer, "clear_placeholder_row", lambda ws, row: None)
        ws = self._worksheet()
        # Ні в збереженій позиції, ні однозначно в колонці нашого наряду нема.
        ws.cell.return_value = SimpleNamespace(value="99999")
        ws.col_values.return_value = ["99999"] * 20

        for _ in range(sheet_erase_guard.LIMIT * 2):
            assert sheet_writer.clear_order_row(ws, self._lab_order()) is False

        assert sheet_erase_guard.recent_count() == 0


class TestRestoreErasedRow:
    """Повернення стертого рядка за вмістом із журналу (B.8). Звірка identity
    тут неможлива — клітинка порожня за визначенням, — тож єдиний запобіжник
    той самий, що й у restore_order_row: рядок мусить бути ПОРОЖНІЙ."""

    def _worksheet(self, current):
        ws = MagicMock()
        ws.id = 11
        ws.get_values.return_value = current
        return ws

    def test_writes_the_saved_values_back(self):
        from app.sheet_writer import restore_erased_row

        ws = self._worksheet([[""] * 11])
        restore_erased_row(ws, 13, ["1", "24122", "2", "моно а3"])

        ws.batch_update.assert_called_once()
        (payload,), _ = ws.batch_update.call_args
        assert payload[0]["range"] == "A13:K13"
        # Хвіст добивається порожніми: інакше в K лишився б старий вміст.
        # Кількість — числом (11.09.26: текст «2» сума таблиці пропускала).
        assert payload[0]["values"][0][:4] == ["1", "24122", 2, "моно а3"]
        assert len(payload[0]["values"][0]) == 11

    def test_occupied_row_is_never_overwritten(self):
        from app.sheet_writer import RowOccupiedError, restore_erased_row

        ws = self._worksheet([["", "99999", "", "цирконій"]])
        with pytest.raises(RowOccupiedError):
            restore_erased_row(ws, 13, ["1", "24122"])
        ws.batch_update.assert_not_called()

    def test_unreadable_row_refuses_instead_of_assuming_empty(self):
        from app.sheet_writer import restore_erased_row

        ws = MagicMock()
        ws.get_values.side_effect = RuntimeError("проксі обірвав зʼєднання")
        with pytest.raises(RuntimeError, match="чи рядок 13 вільний"):
            restore_erased_row(ws, 13, ["1", "24122"])
        ws.batch_update.assert_not_called()

    def test_empty_saved_content_is_not_restorable(self):
        from app.sheet_writer import restore_erased_row

        ws = self._worksheet([[""] * 11])
        with pytest.raises(ValueError):
            restore_erased_row(ws, 13, ["", "  "])
        ws.batch_update.assert_not_called()


# ── Сесія воркера не тримає блокування бази крізь мережевий виклик ─────────
# Аудит 08.09.26. Функції write-back тримають сесію відкритою через розмову з
# Google (холодне відкриття таблиці — до 40 с). Поки в сесії немає незавершених
# записів, це нешкідливо. Але сюди по дорозі лягають рядки SyncLog, і при
# autoflush перший же SELECT змиває їх у базу — тобто відкриває BEGIN IMMEDIATE
# і тримає блокування запису весь час мережевого виклику. Другий оператор у цю
# мить отримає «database is locked» замість екрана видачі.


def test_writeback_session_has_autoflush_off():
    """Головний інваріант: запис у базу відбувається ЛИШЕ там, де ми написали
    `commit()`, і жоден із них не потрапляє всередину мережевого виклику."""
    from app.services.sheet_writeback import writeback_session

    with writeback_session() as session:
        assert session.autoflush is False, (
            "autoflush увімкнено — будь-який SELECT усередині розмови з Google "
            "змиє незавершені SyncLog у базу й триматиме блокування запису"
        )


def test_module_takes_every_session_from_the_single_factory():
    """Одна точка входу, а не дванадцять `SessionLocal()`.

    Якщо десь у модулі знову зʼявиться сесія в обхід фабрики, вона прийде з
    увімкненим autoflush — і поверне проблему рівно там, де її не шукатимуть.
    """
    import inspect

    from app.services import sheet_writeback

    source = inspect.getsource(sheet_writeback)
    assert "SessionLocal(" not in source, (
        "сесія в обхід writeback_session() — вона прийде з autoflush=True"
    )


def test_sync_log_rows_are_committed_immediately():
    """Рядок журналу комітиться одразу, а не чекає кінця функції.

    Журнал описує спробу, а не змінює роботу, тож тримати його незакомічений
    крізь мережу немає причин — зате є наслідок (блокування бази).
    """
    import inspect

    from app.services import sheet_writeback

    source = inspect.getsource(sheet_writeback)
    assert "bg.add(SyncLog(" not in source, (
        "SyncLog додається без негайного коміту — використовуй _log_sync()"
    )
