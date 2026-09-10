"""«Де ця робота?»: чи справді видно, на якому кроці наряд загубився.

Найчастіше питання цеху — «робота є в таблиці, а в черзі її нема». Звичайний
пошук на нього не відповідає принципово: він шукає серед того, що вже є в
базі, а питають саме тоді, коли роботи в базі немає. Тому головні тести тут —
про випадок, коли рядок у копії вкладки Є, а роботи НЕМАЄ.

Дані про таблицю беруться з локальних CSV-знімків (`app/sheet_backup.py`), тож
тести пишуть справжні знімки у тимчасову теку — без мокання формату, який
потім розійдеться з тим, що знімає застосунок.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Order
from app.services import order_trace


HEADER_ROWS = [
    ["", "", "", "", "Всього", "238"],
    ["№", "Номер наряду", "Кількість", "Колір роботи", "Вид работи", "Здати до"],
    [],
    ["", "", "", "", "", "14:00", "16:00", "09:00"],
    [],
    ["1", "2", "", "4", "4а", "5", "6", "7", "8", "9", "10", "11"],
]


def _db() -> Session:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _row(work_order="30150", quantity="2", color="моно а3", kind="анатомія",
         job_code="2026-09-08_00016-007", technician="Денис", sum3d="14-58-11"):
    """Рядок у розкладці цехової таблиці (CLAUDE.md §3)."""
    return [
        "1", work_order, quantity, color, kind, "х", "", "",
        job_code, technician, "", sum3d,
    ]


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    """Справжня тека знімків із справжнім CSV і маніфестом."""
    folder = tmp_path / "backups" / "sheets"
    folder.mkdir(parents=True)

    def write(iso: str, tab: str, rows, taken_at=None, disappeared_at=None):
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in HEADER_ROWS + list(rows):
            writer.writerow(row)
        (folder / f"{iso}.csv").write_text(buf.getvalue(), encoding="utf-8")
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.exists() else {}
        # Маніфест ключується ISO-датою, а НЕ іменем файлу — так його пише
        # сам застосунок (`app/sheet_backup._save_manifest`). Перша версія
        # цієї фікстури ключувала іменем, і метадані мовчки не доїжджали:
        # знімок читався, а «коли знято» лишалось порожнім.
        manifest[iso] = {
            "tab": tab,
            "taken_at": taken_at or datetime.now().isoformat(timespec="seconds"),
            "rows": len(rows),
            **({"disappeared_at": disappeared_at} if disappeared_at else {}),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(order_trace, "DB_PATH", str(tmp_path / "kuubmill.db"))
    return write


def _order(db: Session, **kw) -> Order:
    order = Order(
        source=kw.pop("source", "lab"),
        status=kw.pop("status", "нове"),
        created_at=kw.pop("created_at", datetime.now()),
        **kw,
    )
    db.add(order)
    db.commit()
    return order


# ── Головний випадок: рядок є, роботи немає ────────────────────────────────


def test_a_row_in_the_sheet_without_an_order_is_the_answer(snapshots):
    """Те, заради чого інструмент і зроблено."""
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30150")])
    db = _db()

    result = order_trace.trace(db, "30150")

    assert result.sheet_hits, "рядок у копії вкладки не знайдено"
    assert not result.orders
    assert "загубилась" in result.verdict
    assert any(step.ok is False and "не знайдена" in step.detail for step in result.steps)


def test_an_empty_material_is_named_as_the_reason(snapshots):
    """Найтихіша причина: рядок без матеріалу роботою не вважається, щоб у
    чергу не лізли підсумкові рядки (§14, `OrderRow.is_work_row`)."""
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30151", color="")])
    db = _db()

    result = order_trace.trace(db, "30151")

    why = " ".join(step.detail for step in result.steps)
    assert "матеріал" in why
    assert any("Заповнити" in action for action in result.actions)


def test_an_empty_quantity_is_named_too(snapshots):
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30152", quantity="")])
    db = _db()

    result = order_trace.trace(db, "30152")

    assert "кількост" in " ".join(step.detail for step in result.steps)


def test_a_complete_looking_row_points_at_the_sync(snapshots):
    """Рядок цілий — значить справа не в ньому, і відправляти треба в «Що не
    так», а не вигадувати причину."""
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30153")])
    db = _db()

    result = order_trace.trace(db, "30153")

    why = " ".join(step.detail for step in result.steps)
    assert "синхронізація" in why.lower()


def test_a_vanished_tab_is_named(snapshots):
    snapshots(
        "2026-09-08", "08.09.26", [_row(work_order="30154")],
        disappeared_at="2026-09-09T10:00:00",
    )
    db = _db()

    result = order_trace.trace(db, "30154")

    assert "зникла" in " ".join(step.detail for step in result.steps)
    assert result.sheet_hits[0].disappeared_at


# ── Робота знайдена ─────────────────────────────────────────────────────────


def test_a_live_order_is_reported_as_in_the_queue(snapshots):
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30150")])
    db = _db()
    _order(db, work_order_no="30150", status="прораховано", sheet_tab="08.09.26")

    result = order_trace.trace(db, "30150")

    assert "черз" in result.verdict.lower()
    assert result.orders and result.orders[0].work_order_no == "30150"


def test_an_archived_order_says_when_and_why(snapshots):
    """«Її немає в черзі» і «її немає взагалі» — різні відповіді, і плутати їх
    тут найдорожче."""
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30155")])
    db = _db()
    _order(
        db, work_order_no="30155", sheet_tab="08.09.26",
        archived_at=datetime.now() - timedelta(hours=3),
    )

    result = order_trace.trace(db, "30155")

    assert "архів" in result.verdict.lower()
    assert "Прибрана з черги" in " ".join(step.detail for step in result.steps)


def test_an_old_order_is_archived_by_age_not_by_fault(snapshots):
    """Виїзд за 30-денне вікно — це нормальна робота черги, і казати про неї
    як про поломку не можна: людина піде шукати неіснуючу проблему."""
    snapshots("2026-01-08", "08.01.26", [_row(work_order="30156")])
    db = _db()
    _order(db, work_order_no="30156", sheet_tab="08.01.26")

    result = order_trace.trace(db, "30156")

    joined = " ".join(step.detail for step in result.steps)
    assert "не поломка" in joined


# ── Пошук ───────────────────────────────────────────────────────────────────


def test_search_finds_by_sum3d_and_technician(snapshots):
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30157", sum3d="12-01-45")])
    db = _db()

    assert order_trace.trace(db, "12-01-45").sheet_hits
    assert order_trace.trace(db, "Денис").sheet_hits


def test_search_is_case_insensitive_in_cyrillic(snapshots):
    """`lower()` у SQLite розуміє лише латиницю — тому порівняння в Python."""
    snapshots("2026-09-08", "08.09.26", [_row(technician="Середюк")])
    db = _db()

    assert order_trace.trace(db, "середюк").sheet_hits


def test_header_rows_are_never_matched(snapshots):
    """Заголовки таблиці (рядки 1-6) — не дані. Інакше пошук за «Кількість»
    видавав би рядок-заголовок як знайдену роботу."""
    snapshots("2026-09-08", "08.09.26", [_row()])
    db = _db()

    assert order_trace.trace(db, "Номер наряду").sheet_hits == []


def test_nothing_found_says_so_and_offers_next_steps(snapshots):
    """Фікстура тут потрібна навіть без знімків: без неї тест читав СПРАВЖНЮ
    теку копій цієї машини і залежав від того, що там лежить (перший прогін
    знайшов збіг у чужому рядку)."""
    db = _db()

    result = order_trace.trace(db, "не існує")

    assert "Нічого не знайдено" in result.verdict
    assert result.actions


def test_an_empty_query_does_nothing(snapshots):
    assert order_trace.trace(_db(), "   ").steps == []


# ── Чесність про джерело ────────────────────────────────────────────────────


def test_the_answer_admits_the_snapshot_may_be_stale(snapshots):
    """Копія могла бути знята кілька хвилин тому. Мовчання перетворило б «за
    копією» на «в таблиці» — різні твердження, і на такій підміні розбір іде
    хибним шляхом."""
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30158")])
    db = _db()

    result = order_trace.trace(db, "30158")

    assert result.snapshot_taken_at, "не сказано, коли знято копію"


def test_no_snapshots_at_all_is_not_reported_as_absence(snapshots):
    """«Не знайшли в копіях» ≠ «немає в таблиці». Перше — межа наших знань."""
    db = _db()          # знімків не пишемо навмисно

    result = order_trace.trace(db, "30159")

    joined = " ".join(step.detail for step in result.steps)
    assert "не те саме" in joined


def test_a_broken_snapshot_file_does_not_break_the_search(snapshots, tmp_path):
    """Один зіпсований файл не сміє забрати відповідь: решта копій цілі."""
    snapshots("2026-09-08", "08.09.26", [_row(work_order="30160")])
    (tmp_path / "backups" / "sheets" / "2026-09-07.csv").write_bytes(b"\xff\xfe\x00binary")
    manifest = tmp_path / "backups" / "sheets" / "manifest.json"
    data = json.loads(manifest.read_text("utf-8"))
    data["2026-09-07"] = {"tab": "07.09.26", "taken_at": "2026-09-08T10:00:00", "rows": 1}
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    db = _db()

    result = order_trace.trace(db, "30160")

    assert result.sheet_hits, "цілий знімок загубився через сусідній зіпсований"
