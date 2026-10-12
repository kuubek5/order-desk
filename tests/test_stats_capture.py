"""Етап 0 «Статистики» (STATS_BRIEF.md §3): два моменти, які треба ПОЧАТИ
записувати, бо заднім числом їх не відновиш.

  * `Order.row_seen_at` — коли рядок зʼявився в таблиці. Лише звичайний тік
    синку поточної вкладки й ручне додавання; імпорт історії, перший імпорт,
    воскресіння й перевикористаний рядок — NULL («невідомо»), не хибний час.
  * `Order.job_code_at` — коли технік уперше вписав «Номер роботи» (шлях).
    Народжена зі шляхом — NULL; повторне заповнення час не переписує; рядок,
    відданий іншій роботі, — NULL.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.business_day import business_today, next_tab_day, prev_tab_day, tab_day
from app.db import Base
from app.models import Order, User
from app.parser import OrderRow
from app.sheet_sync_service import _stats_stamps, sync_google_sheets, sync_hot_tab
from app.sheets import reset_sheets_cache
from app.sync import sync_tab

ROOT = Path(__file__).resolve().parent.parent
PINNED = datetime(2026, 10, 12, 10, 15, 30)


@pytest.fixture(autouse=True)
def _pin_local_now(monkeypatch):
    """Годинник моментів — одна точка, щоб звіряти рівністю."""
    monkeypatch.setattr("app.sync._local_now", lambda: PINNED)


def _session() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _row(**overrides) -> OrderRow:
    values = {
        "row_number": 1, "seq_no": "1", "work_order_no": "24122", "quantity": "2",
        "material_color": "моно A2", "kind": "анатомія", "due_time": "14:00",
        "job_code": "", "technician_name": "Технік", "cam_comment": "",
        "sum3d_id": "", "calculated": "", "milled": "", "last_milled_date": "",
        "mill_count": "",
    }
    values.update(overrides)
    return OrderRow(**values)


def _only(session: Session) -> Order:
    orders = session.scalars(select(Order)).all()
    assert len(orders) == 1
    return orders[0]


# --- row_seen_at на рівні sync_tab ------------------------------------------


def test_live_tick_stamps_row_seen_at_on_creation():
    with _session() as s:
        sync_tab(s, "12.10.26", [_row()], stamp_row_seen=True)
        s.commit()
        assert _only(s).row_seen_at == PINNED


def test_without_the_flag_row_seen_at_stays_unknown():
    with _session() as s:
        sync_tab(s, "12.10.26", [_row()])
        s.commit()
        assert _only(s).row_seen_at is None


def test_later_ticks_do_not_move_row_seen_at():
    with _session() as s:
        sync_tab(s, "12.10.26", [_row()], stamp_row_seen=True)
        s.commit()
        first = _only(s).row_seen_at
        sync_tab(s, "12.10.26", [_row(cam_comment="правка")], stamp_row_seen=True)
        s.commit()
        assert _only(s).row_seen_at == first


def test_resurrection_of_same_work_does_not_stamp():
    """Та сама робота повертається з архіву — момент появи рядка невідомий."""
    with _session() as s:
        s.add(Order(
            source="lab", sheet_tab="12.10.26", row_number=1, work_order_no="24122",
            material_color="моно A2", quantity="2", kind="анатомія",
            technician_name="Технік", status="нове",
            archived_at=datetime.utcnow() - timedelta(hours=1),
        ))
        s.commit()
        sync_tab(s, "12.10.26", [_row(job_code="P:/шлях")],
                 stamp_row_seen=True, stamp_job_code=True)
        s.commit()
        order = _only(s)
        assert order.archived_at is None, "передумова: робота воскресла"
        assert order.row_seen_at is None
        assert order.job_code_at is None, "шлях вписали, поки роботу не бачили"


def test_row_given_to_another_work_resets_both_moments():
    """Перевикористаний рядок: нова робота — момент невідомий, шлях прийшов з нею."""
    with _session() as s:
        s.add(Order(
            source="lab", sheet_tab="12.10.26", row_number=1, work_order_no="11111",
            material_color="TEST", status="нове",
            row_seen_at=datetime(2026, 10, 1, 9, 0), job_code_at=datetime(2026, 10, 1, 9, 5),
            archived_at=datetime.utcnow() - timedelta(hours=1),
        ))
        s.commit()
        sync_tab(s, "12.10.26", [_row(work_order_no="28393", job_code="P:/нова")],
                 stamp_row_seen=True, stamp_job_code=True)
        s.commit()
        order = _only(s)
        assert order.work_order_no == "28393", "передумова: рядок віддано іншій роботі"
        assert order.row_seen_at is None
        assert order.job_code_at is None


def test_kind_flip_on_live_order_resets_moments():
    """Рядок лабораторної роботи переписали під клієнта (`existing.source !=
    source`) — це інша робота, її моменти невідомі."""
    with _session() as s:
        s.add(Order(
            source="lab", sheet_tab="12.10.26", row_number=5, work_order_no="11111",
            status="нове", row_seen_at=datetime(2026, 10, 12, 8, 0),
        ))
        s.commit()
        client = _row(row_number=5, work_order_no="", technician_name="", kind="Басараб",
                      material_color="mono a3", quantity="1", job_code="P:/клієнт")
        assert client.is_client_row
        sync_tab(s, "12.10.26", [client], row_fills={5: "blue"},
                 stamp_row_seen=True, stamp_job_code=True)
        s.commit()
        order = _only(s)
        assert order.source == "sheet_client", "передумова: рядок став клієнтським"
        assert order.row_seen_at is None
        assert order.job_code_at is None


# --- job_code_at ------------------------------------------------------------


def test_first_job_code_fill_stamps_job_code_at():
    with _session() as s:
        sync_tab(s, "12.10.26", [_row()], stamp_job_code=True)
        s.commit()
        assert _only(s).job_code_at is None
        sync_tab(s, "12.10.26", [_row(job_code="2026-10-12_00001-001")], stamp_job_code=True)
        s.commit()
        assert _only(s).job_code_at == PINNED


def test_born_with_path_keeps_job_code_at_null():
    with _session() as s:
        sync_tab(s, "12.10.26", [_row(job_code="P:/шлях")],
                 stamp_row_seen=True, stamp_job_code=True)
        s.commit()
        sync_tab(s, "12.10.26", [_row(job_code="P:/шлях")], stamp_job_code=True)
        s.commit()
        assert _only(s).job_code_at is None


def test_refill_after_clear_keeps_the_first_time(monkeypatch):
    with _session() as s:
        sync_tab(s, "12.10.26", [_row()], stamp_job_code=True)
        sync_tab(s, "12.10.26", [_row(job_code="P:/перший")], stamp_job_code=True)
        s.commit()
        assert _only(s).job_code_at == PINNED

        monkeypatch.setattr("app.sync._local_now", lambda: PINNED + timedelta(hours=2))
        sync_tab(s, "12.10.26", [_row(job_code="")], stamp_job_code=True)
        sync_tab(s, "12.10.26", [_row(job_code="P:/другий")], stamp_job_code=True)
        s.commit()
        order = _only(s)
        assert order.job_code == "P:/другий"
        assert order.job_code_at == PINNED, "рахуємо перше здавання"


def test_job_code_fill_outside_a_regular_tick_is_not_stamped():
    with _session() as s:
        sync_tab(s, "12.10.26", [_row()])
        sync_tab(s, "12.10.26", [_row(job_code="P:/шлях")])
        s.commit()
        assert _only(s).job_code_at is None


# --- хто вирішує «звичайний тік» --------------------------------------------


def test_stats_stamps_window():
    today = date(2026, 10, 14)  # середа
    title = lambda d: d.strftime("%d.%m.%y")  # noqa: E731
    current = tab_day(today)
    assert _stats_stamps(title(current), work_today=today, regular_tick=True) == (True, True)
    assert _stats_stamps(title(prev_tab_day(current)), work_today=today, regular_tick=True) == (False, True)
    assert _stats_stamps(title(next_tab_day(current)), work_today=today, regular_tick=True) == (False, True)
    assert _stats_stamps(title(current - timedelta(days=5)), work_today=today, regular_tick=True) == (False, False)
    assert _stats_stamps(title(current), work_today=today, regular_tick=False) == (False, False)
    assert _stats_stamps("Підсумок", work_today=today, regular_tick=True) == (False, False)


def test_weekend_current_tab_is_fridays():
    saturday = date(2026, 10, 17)
    friday = "16.10.26"
    assert _stats_stamps(friday, work_today=saturday, regular_tick=True) == (True, True)


def _configured(monkeypatch, spreadsheet):
    monkeypatch.setattr("app.sheet_sync_service.get_google_sheet_id", lambda session: "id")
    monkeypatch.setattr(
        "app.sheet_sync_service.get_google_service_account_json",
        lambda session: '{"type": "service_account"}',
    )
    monkeypatch.setattr("app.sheet_sync_service.open_spreadsheet", lambda db: spreadsheet)
    monkeypatch.setattr("app.sheet_sync_service.fetch_row_fills", lambda ws: None)


def _worksheet(day: date, rows: list[list[str]]) -> Mock:
    ws = Mock()
    ws.title = day.strftime("%d.%m.%y")
    ws.get_all_values.return_value = ([[]] * 6) + rows
    return ws


@pytest.mark.usefixtures("weekday_clock")
def test_first_import_does_not_stamp_but_a_later_tick_does(monkeypatch):
    """Перший імпорт на порожній базі створює сьогоднішні рядки пачкою — час
    імпорту не є часом появи. Наступний звичайний синк ставить новому рядку."""
    today = business_today()
    # Сусідня вкладка — ЗАВТРАШНЯ: її повний синк читає завжди (у понеділок
    # «вчора» за календарем — неділя, а п'ятниця вже поза його вікном).
    current = _worksheet(today, [["1", "100", "2", "моно A2", "анатомія", "x"]])
    past = _worksheet(today + timedelta(days=1), [["1", "200", "1", "пмма A3", "коронка", "x"]])
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [past, current]
    _configured(monkeypatch, spreadsheet)

    with _session() as s:
        sync_google_sheets(s)
        assert {o.work_order_no: o.row_seen_at for o in s.scalars(select(Order))} == {
            "100": None, "200": None,
        }

        current.get_all_values.return_value = ([[]] * 6) + [
            ["1", "100", "2", "моно A2", "анатомія", "x"],
            ["2", "101", "1", "моно A3", "анатомія", "x"],
        ]
        past.get_all_values.return_value = ([[]] * 6) + [
            ["1", "200", "1", "пмма A3", "коронка", "x"],
            ["2", "201", "1", "пмма A2", "коронка", "x"],
        ]
        sync_google_sheets(s)
        seen = {o.work_order_no: o.row_seen_at for o in s.scalars(select(Order))}
        assert seen["101"] == PINNED, "новий рядок поточної вкладки на звичайному синку"
        assert seen["201"] is None, "завтрашня вкладка — не поточна"
        assert seen["100"] is None and seen["200"] is None


@pytest.mark.usefixtures("weekday_clock")
def test_full_history_import_never_stamps(monkeypatch):
    today = business_today()
    current = _worksheet(today, [["1", "100", "2", "моно A2", "анатомія", "x"]])
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [current]
    _configured(monkeypatch, spreadsheet)

    with _session() as s:
        s.add(Order(source="lab", sheet_tab="01.01.26", row_number=1, status="нове"))
        s.commit()
        sync_google_sheets(s, full_history=True)
        order = s.scalar(select(Order).where(Order.work_order_no == "100"))
        assert order is not None and order.row_seen_at is None


@pytest.mark.usefixtures("weekday_clock")
def test_hot_tick_stamps_only_the_current_tab(monkeypatch):
    reset_sheets_cache()
    today = business_today()
    current = _worksheet(today, [["1", "300", "2", "моно A2", "анатомія", "x"]])
    past = _worksheet(prev_tab_day(today), [["1", "400", "1", "пмма A3", "коронка", "x"]])
    by_name = {current.title: current, past.title: past}
    spreadsheet = Mock()
    spreadsheet.worksheets.return_value = [current, past]
    spreadsheet.worksheet.side_effect = lambda name: by_name[name]
    _configured(monkeypatch, spreadsheet)

    with _session() as s:
        s.add(Order(source="lab", sheet_tab="01.01.26", row_number=1, status="нове"))
        s.commit()
        sync_hot_tab(s, today=today)
        seen = {o.work_order_no: o.row_seen_at for o in s.scalars(select(Order))}
        assert seen["300"] == PINNED
        assert seen["400"] is None
    reset_sheets_cache()


# --- ручне додавання ---------------------------------------------------------


def test_manual_add_stamps_row_seen_at_and_not_job_code_at():
    from app.services.manual_add import create_manual_batch

    def write_rows(day, works, *, paint_blue, placement, target_tab):
        return "12.10.26", list(range(60, 60 + len(works)))

    with _session() as db:
        user = User(username="op", password_hash="x", full_name="Оп", role="оператор")
        db.add(user)
        db.commit()
        before = datetime.now() - timedelta(minutes=1)
        result = create_manual_batch(
            db, user=user, work_type="client", target_tab="",
            client_name=["Басараб"], work_order_no=[""], kind=[""],
            material_color=["mono a3"], quantity=["1"], sum3d_id=[""],
            job_code=["P:/шлях"], technician_name=[""], opak=[""],
            write_rows=write_rows,
        )
        assert result.error is None
        order = db.get(Order, result.created_ids[0])
        assert order.row_seen_at is not None and order.row_seen_at.tzinfo is None
        assert before <= order.row_seen_at <= datetime.now() + timedelta(hours=4)
        assert order.job_code_at is None


# --- схема -------------------------------------------------------------------


def test_fresh_database_has_the_columns_in_a_clean_interpreter(tmp_path):
    """Як лаунчер: `ensure_schema` до будь-якого імпорту моделей (CLAUDE.md
    §14 «Аварійне відновлення»)."""
    db_path = tmp_path / "fresh.db"
    code = (
        "import sqlite3\n"
        "from pathlib import Path\n"
        "from app.schema import ensure_schema\n"
        f"ensure_schema(Path({str(db_path)!r}), Path({str(tmp_path / 'backups')!r}))\n"
        f"c = sqlite3.connect({str(db_path)!r})\n"
        "cols = {r[1] for r in c.execute('PRAGMA table_info(orders)')}\n"
        "print('row_seen_at' in cols, 'job_code_at' in cols)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT,
        env=dict(os.environ, DB_PATH=str(db_path), PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr[-600:]
    assert result.stdout.split()[-2:] == ["True", "True"], result.stdout


def test_migration_adds_the_columns_to_an_existing_database(tmp_path):
    db_path = tmp_path / "old.db"
    env = dict(os.environ, DB_PATH=str(db_path), PYTHONIOENCODING="utf-8")

    def alembic(*args: str) -> None:
        done = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert done.returncode == 0, done.stderr[-600:]

    import sqlite3

    def columns() -> set[str]:
        c = sqlite3.connect(db_path)
        try:
            return {r[1] for r in c.execute("PRAGMA table_info(orders)")}
        finally:
            c.close()

    alembic("upgrade", "0088_user_seen_hello_day")
    assert "row_seen_at" not in columns()
    alembic("upgrade", "0089_order_stats_moments")
    assert {"row_seen_at", "job_code_at"} <= columns()
    alembic("downgrade", "0088_user_seen_hello_day")
    assert "row_seen_at" not in columns() and "job_code_at" not in columns()
