"""Груповий Sum3D у доці екрана «Нові з пошти» (власник 13.10.26).

Лабораторні рядки в доці («Лише лаба», 0.21.57) рахують так само, як листи:
кілька робіт — ОДИН проєкт Sum3D. У черзі для цього є шпилька «мої зараз» і
смуга «Призначити всім» (`/orders/sum3d-batch`); у доці шпилька була, смуги —
ні. Тепер та сама смуга стоїть у доці, форма htmx-ова (`ctx=mail`), правила
призначення — спільні з чергою (`_sum3d_batch_assign`).

Що ламається тихо:
- док-смуга, що після першої шпильки перепитує себе БЕЗ `?ctx=mail`, повертає
  чергову форму з редіректом у чергу — оператор вилітає з пошти;
- пачка мусить лягти в таблицю ОДНИМ звірянням і ОДНИМ записом (`write_fields_bulk`),
  а позначка «ще не в таблиці» — зніматись лише на підтвердженому записі;
- робота з уже вписаним Sum3D НЕ перезаписується (те саме правило, що в черзі);
- черга лишається на редіректі (форма без ctx).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.business_day import business_tab_today
from app.models import Order, User
from app.routers import orders as orders_router
from app.services import sheet_stuck_writes
from app.services import sheet_writeback as wb
from app.services.focus import focused_ids, toggle
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")

HX = {"HX-Request": "true"}


def _tab() -> str:
    return business_tab_today().strftime("%d.%m.%y")


def _lab(db, no, row, *, sum3d=None):
    order = Order(
        source="lab", work_order_no=no, material_color="моно а3", quantity="2",
        status="нове", sheet_tab=_tab(), row_number=row,
        job_code=f"2026-10-13_{no}", sum3d_id=sum3d,
    )
    db.add(order)
    db.commit()
    return order


def _mail(db, client):
    order = Order(
        source="email", client_name=client, material_color="pmma a2", quantity="1",
        status="прийнято", sheet_tab=_tab(),
    )
    db.add(order)
    db.commit()
    return order


def _operator(db) -> User:
    return db.scalar(select(User).where(User.username == OPERATOR[0]))


def _seed(session_factory, *, with_taken=True):
    """Три лабораторні рядки + один лист, усі пришпилені; одна лаба вже має Sum3D."""
    with session_factory() as db:
        op = _operator(db)
        op.mail_dock_scope = "lab"
        a = _lab(db, "31001", 7)
        b = _lab(db, "31002", 8)
        m = _mail(db, "Струбицький")
        pinned = [a, b, m]
        taken = None
        if with_taken:
            taken = _lab(db, "31003", 9, sum3d="09-00-00")
            pinned.append(taken)
        for order in pinned:
            toggle(db, order, op)
        db.commit()
        return {"a": a.id, "b": b.id, "m": m.id, "taken": taken.id if taken else None}


@pytest.fixture
def captured(monkeypatch):
    """Межа з Google — пачка `queue_sheet_fields` перехоплюється, а не йде у
    воркер; тест сам віддає її `write_fields_bulk`, як це зробив би воркер."""
    batch: dict[int, tuple[set[str], set[str]]] = {}
    calls: list[int] = []

    def fake_queue(order_id, fields, erase=frozenset()):
        calls.append(order_id)
        have_f, have_e = batch.get(order_id, (set(), set()))
        batch[order_id] = (have_f | set(fields), have_e | set(erase))

    monkeypatch.setattr(orders_router, "queue_sheet_fields", fake_queue)
    return SimpleNamespace(batch=batch, calls=calls)


def _wire_google(monkeypatch, session_factory, *, write_raises=False):
    """Підміна рівно межі з Google: відкриття, звірка рядків, запис."""
    engine = session_factory().get_bind()
    monkeypatch.setattr(
        wb, "writeback_session",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    calls = {"resolve": 0, "write": 0, "plans": []}
    monkeypatch.setattr(wb, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(wb, "get_worksheet_by_name", lambda ss, name: SimpleNamespace(id=1, title=name))

    def fake_resolve(ws, orders):
        calls["resolve"] += 1
        return {order.id: order.row_number for order in orders}

    def fake_write(ws, plan):
        calls["write"] += 1
        calls["plans"].append(sorted(order.id for order, _, _ in plan))
        if write_raises:
            raise RuntimeError("Google сказав ні")

    monkeypatch.setattr(wb, "resolve_rows_bulk", fake_resolve)
    monkeypatch.setattr(wb, "write_order_fields_bulk", fake_write)
    return calls


def _login(app) -> MiniClient:
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303), status
    return client


def test_mail_page_shows_the_batch_bar_in_the_dock_as_htmx_form(app_db):  # noqa: F811
    app, session_factory = app_db
    _seed(session_factory)
    client = _login(app)

    status, _, page = client.get("/mail")
    assert status == 200, status
    dock = page[page.index('id="mail-queue-mirror"'):]
    assert 'class="qmir-batch"' in dock, "смуга стоїть у доці"
    assert 'hx-get="/orders/focus/bar?ctx=mail"' in dock, "самоперепит лишається в контексті пошти"
    assert 'hx-post="/orders/sum3d-batch"' in dock
    assert 'name="ctx" value="mail"' in dock
    assert "Призначити всім" in dock
    # Смуга поза тілом, яке свапає полл, — недописаний ID не зникає щотіку.
    assert dock.index('class="qmir-batch"') < dock.index('id="qmir-body"')
    assert "mail-dock-refresh from:body" in dock


def test_dock_bar_hides_below_two_pins_and_keeps_its_context(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        op = _operator(db)
        toggle(db, _lab(db, "31001", 7), op)
        db.commit()
    client = _login(app)

    status, _, page = client.get("/mail")
    assert status == 200
    assert 'hx-get="/orders/focus/bar?ctx=mail"' in page
    dock = page[page.index('id="mail-queue-mirror"'):]
    assert "Призначити всім" not in dock, "одна шпилька — смуги немає, ID простіше вписати в рядок"

    status, _, bar = client.get("/orders/focus/bar?ctx=mail")
    assert status == 200 and 'hx-get="/orders/focus/bar?ctx=mail"' in bar
    status, _, bar = client.get("/orders/focus/bar")
    assert status == 200 and "ctx=mail" not in bar, "черга лишається без контексту пошти"


def test_one_sum3d_lands_on_all_pinned_lab_rows_in_one_bulk_write(  # noqa: F811
    app_db, captured, monkeypatch  # noqa: F811
):
    app, session_factory = app_db
    ids = _seed(session_factory)
    google = _wire_google(monkeypatch, session_factory)
    sheet_stuck_writes.reset()
    client = _login(app)

    status, headers, html = client.post(
        "/orders/sum3d-batch", {"sum3d_id": " 12-01-45 ", "ctx": "mail"}, headers=HX
    )
    assert status == 200, status
    trigger = json.loads(headers["hx-trigger"])
    assert trigger["mail-dock-refresh"] is True
    assert trigger["toast"]["kind"] == "success"
    assert "3 роботам" in trigger["toast"]["message"] and "1 вже взято" in trigger["toast"]["message"]
    # Відповідь — сама смуга, у контексті пошти (не редірект у чергу).
    assert 'id="sum3d-batch-slot"' in html and "ctx=mail" in html

    with session_factory() as db:
        a, b, m, t = (db.get(Order, ids[k]) for k in ("a", "b", "m", "taken"))
        assert (a.sum3d_id, b.sum3d_id, m.sum3d_id) == ("12-01-45",) * 3
        assert t.sum3d_id == "09-00-00", "уже взяту роботу мовчки не перезаписано"
        # Лаба має рядок → «ще не в таблиці», доки таблиця не підтвердила.
        assert a.sum3d_pending == b.sum3d_pending == "12-01-45"
        # Пошта без row_number рядка не має — позначки теж (order_writes_to_sheet).
        assert m.sum3d_pending is None
        assert focused_ids(db, _operator(db)) == {ids["taken"]}, "шпильки знято лише з призначених"

    # Кожна призначена робота стала в ОДНУ пачку; уже взята — ні.
    assert sorted(captured.calls) == sorted([ids["a"], ids["b"], ids["m"]])
    assert all("sum3d_id" in fields for fields, _ in captured.batch.values())

    # Воркер пише пачку: одна звірка позицій, один запис, лише роботи з рядком.
    error = wb.write_fields_bulk(dict(captured.batch))
    assert error is None, error
    assert google["resolve"] == 1 and google["write"] == 1
    assert google["plans"] == [sorted([ids["a"], ids["b"]])]
    with session_factory() as db:
        assert db.get(Order, ids["a"]).sum3d_pending is None
        assert db.get(Order, ids["b"]).sum3d_pending is None
    assert sheet_stuck_writes.failed_sheet_writes() == []


def test_failed_bulk_write_keeps_the_mark_and_raises_the_banner(  # noqa: F811
    app_db, captured, monkeypatch  # noqa: F811
):
    app, session_factory = app_db
    ids = _seed(session_factory, with_taken=False)
    _wire_google(monkeypatch, session_factory, write_raises=True)
    sheet_stuck_writes.reset()
    client = _login(app)

    status, _, _ = client.post(
        "/orders/sum3d-batch", {"sum3d_id": "12-01-45", "ctx": "mail"}, headers=HX
    )
    assert status == 200

    error = wb.write_fields_bulk(dict(captured.batch))
    assert error and "Google сказав ні" in error
    with session_factory() as db:
        # Збій ≠ успіх: позначка лишається, фоновий повтор допише.
        assert db.get(Order, ids["a"]).sum3d_pending == "12-01-45"
        assert db.get(Order, ids["b"]).sum3d_pending == "12-01-45"
    failed = {(item.kind, item.order_id) for item in sheet_stuck_writes.failed_sheet_writes()}
    assert failed == {("sum3d", ids["a"]), ("sum3d", ids["b"])}
    sheet_stuck_writes.reset()


def test_all_pinned_already_taken_writes_nothing_and_says_so(  # noqa: F811
    app_db, captured  # noqa: F811
):
    app, session_factory = app_db
    with session_factory() as db:
        op = _operator(db)
        for no, row in (("31001", 7), ("31002", 8)):
            toggle(db, _lab(db, no, row, sum3d="09-00-00"), op)
        db.commit()
    client = _login(app)

    status, headers, _ = client.post(
        "/orders/sum3d-batch", {"sum3d_id": "12-01-45", "ctx": "mail"}, headers=HX
    )
    assert status == 200
    trigger = json.loads(headers["hx-trigger"])
    assert trigger["toast"]["kind"] == "warning"
    assert captured.calls == []
    with session_factory() as db:
        assert {o.sum3d_id for o in db.scalars(select(Order))} == {"09-00-00"}


def test_empty_id_from_the_dock_is_a_toast_not_a_redirect(app_db, captured):  # noqa: F811
    app, session_factory = app_db
    _seed(session_factory, with_taken=False)
    client = _login(app)

    status, headers, _ = client.post(
        "/orders/sum3d-batch", {"sum3d_id": "  ", "ctx": "mail"}, headers=HX
    )
    assert status == 200
    assert json.loads(headers["hx-trigger"])["toast"]["kind"] == "error"
    assert captured.calls == []


def test_queue_form_without_ctx_still_redirects(app_db, captured):  # noqa: F811
    app, session_factory = app_db
    _seed(session_factory, with_taken=False)
    client = _login(app)

    status, headers, _ = client.post("/orders/sum3d-batch", {"sum3d_id": "12-01-45"})
    assert status == 303
    assert headers.get("location") == "/"
    assert len(captured.calls) == 3
