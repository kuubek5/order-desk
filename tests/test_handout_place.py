"""«Де лежить робота» на ранковій видачі (HANDOUT_PLACE_BRIEF.md, 08.10.26).

Що стережемо:
* `place_of` — текст колонки «Відфрезерував» → піч / верстат / інше / нічого;
  старий маркер авто-літери місцем НЕ є;
* мітка печі лягає на ВЕСЬ диск (Sum3D + вкладка), у всіх клієнтів, і не чіпає
  той самий Sum3D іншого дня й лабораторні рядки;
* запис у таблицю — лише колонка N, явним значенням, тільки підтверджені рядки;
* верстат із телеметрії — лише для робіт дня поруч із датою програми;
* справжній рендер видачі в обох режимах несе мітки, смугу й правильні
  лічильники.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ActionLog, Order, SyncLog, User
from app.parser import HEADER_ROWS
from app.services import handout_place as hp
from app.services import sheet_writeback as writeback_service
from app.sheet_writer import COL_MILLED, apply_status_markers
from tests.asgi_client import MiniClient
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

MACHINES = ["250i-Sec", "250i-Tolik", "350i-Boris", "150i-Olejka", "350i Loader"]


# ── place_of ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, kind, key",
    [
        ("1", "furnace", "p1"),
        (" 1 ", "furnace", "p1"),
        ("бочка", "furnace", "bk"),
        ("Бочка", "furnace", "bk"),
        ("3", "furnace", "p3"),
        ("чіна", "furnace", "cn"),
        ("China", "furnace", "cn"),
        ("250-sec", "machine", "250i-Sec"),
        ("250i-Sec", "machine", "250i-Sec"),
        ("350 loader", "machine", "350i Loader"),
        # Так пишуть у цеху (dev-копія таблиці, 08.10.26): ім'я без моделі й з одруківкою.
        ("Sec", "machine", "250i-Sec"),
        ("Olejka", "machine", "150i-Olejka"),
        ("Loder", "machine", "350i Loader"),
        ("Борис", "machine", "350i-Boris"),
        ("бочка 22:00", "furnace", "bk"),
        ("лоток у шафі", "other", "other"),
    ],
)
def test_place_of_reads_the_cell(raw, kind, key):
    place = hp.place_of(raw, MACHINES)
    assert place is not None
    assert (place.kind, place.key) == (kind, key)


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "RR 09:12", "Роман 11:30", "Claude Dev 23:54", "D", "ЦЦ", "+", "✓"]
)
def test_place_of_ignores_operator_markers_and_empties(raw):
    """Слід авто-літери (до 08.10.26 CRM сама писала «<оператор> HH:MM») і
    літера оператора — не піч. Показати їх місцем означало б збрехати."""
    assert hp.place_of(raw, MACHINES) is None


def test_unknown_text_is_kept_as_is():
    place = hp.place_of("на столі в Олега", MACHINES)
    assert place.kind == "other" and place.label == "на столі в Олега"


def test_status_no_longer_stamps_the_milled_column():
    order = Order(calculated_raw=None, milled_raw=None)
    fields = apply_status_markers(order, "видано", "Роман", datetime(2026, 10, 8, 9, 0))
    assert fields == {"calculated_raw"}
    assert order.milled_raw is None


# ── мітка на диск ───────────────────────────────────────────────────────


def _db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine


def _work(client, sum3d, tab="07.10.26", row=60, source="sheet_client", **kw):
    return Order(
        source=source, sheet_tab=tab, row_number=row, client_name=client,
        material_color="mono a3", quantity="1", status="відфрезеровано", sum3d_id=sum3d, **kw,
    )


def test_furnace_mark_covers_the_whole_disc_and_only_that_day():
    engine = _db()
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="kos", password_hash="x", role="оператор")
        a = _work("Дент-Профі", "21-22-23", row=60)
        b = _work("Лаб. Північ", "21-22-23", row=61)
        other_day = _work("Дент-Профі", "21-22-23", tab="06.10.26", row=62)
        lab = _work(None, "21-22-23", row=8, source="lab", work_order_no="34203")
        other_disc = _work("Сміл", "22-34-07", row=63)
        db.add_all([user, a, b, other_day, lab, other_disc])
        db.commit()

        changed = hp.set_disc_place(db, user, a, "bk")
        db.commit()

        assert {o.id for o in changed} == {a.id, b.id}
        assert a.milled_raw == b.milled_raw == "бочка"
        assert other_day.milled_raw is None, "той самий Sum3D іншого дня — інший диск"
        assert lab.milled_raw is None, "лабораторний рядок не чіпаємо (синк читав би N як «відфрезеровано»)"
        assert other_disc.milled_raw is None
        log = db.scalars(select(ActionLog).where(ActionLog.action_type == "place")).all()
        assert len(log) == 2 and all(e.operator_id == user.id for e in log)

        # Повторний клік тією самою піччю нічого не міняє.
        assert hp.set_disc_place(db, user, b, "bk") == []
        # «Прибрати мітку» — знімає з усього диска.
        cleared = hp.set_disc_place(db, user, b, "")
        assert {o.id for o in cleared} == {a.id, b.id}
        assert a.milled_raw is None and b.milled_raw is None


def test_work_without_sum3d_is_marked_alone():
    engine = _db()
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="kos", password_hash="x", role="оператор")
        a = _work("A", None, row=60)
        b = _work("B", None, row=61)
        db.add_all([user, a, b])
        db.commit()
        assert [o.id for o in hp.set_disc_place(db, user, a, "p1")] == [a.id]
        assert b.milled_raw is None


def test_unknown_furnace_key_is_refused():
    with pytest.raises(ValueError):
        hp.set_disc_place(MagicMock(), None, Order(), "p9")


# ── запис у таблицю ─────────────────────────────────────────────────────


def _stub_sheet(monkeypatch, engine, rows):
    ws = SimpleNamespace(batch_update=MagicMock())
    monkeypatch.setattr(writeback_service, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(writeback_service, "get_worksheet_by_name", lambda ss, name: ws)
    monkeypatch.setattr(
        writeback_service, "writeback_session",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    monkeypatch.setattr(writeback_service, "resolve_rows_bulk", lambda worksheet, orders: {o.id: rows(o) for o in orders})
    return ws


def test_place_write_touches_only_column_n_of_confirmed_rows(monkeypatch):
    engine = _db()
    with Session(engine, expire_on_commit=False) as db:
        a = _work("A", "21-22-23", row=60)
        b = _work("B", "21-22-23", row=61)
        db.add_all([a, b])
        db.commit()
        ids = [a.id, b.id]
        b_id = b.id

    # Рядок B не підтвердився — його пропускаємо, а не пишемо навмання.
    ws = _stub_sheet(monkeypatch, engine, lambda o: None if o.id == b_id else o.row_number + HEADER_ROWS)
    error = writeback_service.write_place_cells_warm(ids, "бочка")

    assert error and "не підтверджено" in error
    (updates,), _ = ws.batch_update.call_args
    assert updates == [{"range": f"N{60 + HEADER_ROWS}", "values": [["бочка"]]}]
    assert COL_MILLED == 14
    with Session(engine) as db:
        statuses = sorted(r.status for r in db.scalars(select(SyncLog)))
        assert statuses == ["ok", "skipped"]


def test_place_write_clears_with_empty_value(monkeypatch):
    engine = _db()
    with Session(engine, expire_on_commit=False) as db:
        a = _work("A", "21-22-23", row=60, milled_raw="1")
        db.add(a)
        db.commit()
        ids = [a.id]
    ws = _stub_sheet(monkeypatch, engine, lambda o: o.row_number + HEADER_ROWS)
    assert writeback_service.write_place_cells_warm(ids, "") is None
    (updates,), _ = ws.batch_update.call_args
    assert updates[0]["values"] == [[""]]
    with Session(engine) as db:
        assert db.get(Order, ids[0]).milled_raw is None


def test_mail_work_without_a_row_is_not_written(monkeypatch):
    engine = _db()
    with Session(engine, expire_on_commit=False) as db:
        a = _work("A", "21-22-23", row=None, source="email")
        db.add(a)
        db.commit()
        ids = [a.id]
    ws = _stub_sheet(monkeypatch, engine, lambda o: 70)
    assert writeback_service.write_place_cells_warm(ids, "1") is None
    ws.batch_update.assert_not_called()


# ── верстат з телеметрії ────────────────────────────────────────────────


def _card(name, sum3d, iso, percent=42):
    return SimpleNamespace(
        sum3d_id=sum3d, percent=percent, target=SimpleNamespace(name=name),
        state=SimpleNamespace(iso_name=iso),
    )


def test_machine_place_only_for_works_near_the_program_date():
    today_work = _work("A", "23-42-52", tab="07.10.26")
    today_work.id = 1
    old_work = _work("B", "23-42-52", tab="01.09.26")
    old_work.id = 2
    card = _card("350i-Boris", "23-42-52", "3_20-Monolith-A3-x45_2026-10-07_23-42-52.iso", percent=33)

    found = hp.machine_places([card], [today_work, old_work])

    assert set(found) == {1}, "Sum3D — лише час доби; робота місячної давнини — чужа"
    assert found[1].label == "350i-Boris" and found[1].percent == 33 and found[1].live


def test_machine_place_needs_a_dated_program_and_one_machine():
    work = _work("A", "23-42-52", tab="07.10.26")
    work.id = 1
    undated = _card("250i-Sec", "23-42-52", None)
    assert hp.machine_places([undated], [work]) == {}
    twins = [
        _card("250i-Sec", "23-42-52", "2026-10-07_23-42-52.iso"),
        _card("350i-Boris", "23-42-52", "2026-10-07_23-42-52.iso"),
    ]
    assert hp.machine_places(twins, [work]) == {}, "двоє верстатів з одним ID — не вгадуємо"


def test_machine_wins_over_the_cell_and_strip_counts():
    works = []
    for i, (sum3d, raw) in enumerate(
        [("a", "1"), ("a", "1"), ("b", "бочка"), ("c", None), ("d", "RR 09:12"), ("e", "1")], start=1
    ):
        w = _work(f"K{i}", sum3d, tab="07.10.26", milled_raw=raw)
        w.id = i
        works.append(w)
    card = _card("250i-Sec", "e", "2026-10-07_e.iso")  # дата є, Sum3D збігається з хвостом
    card.state.iso_name = "x_2026-10-07_10-00-00.iso"
    card.sum3d_id = "10-00-00"
    works[5].sum3d_id = "10-00-00"
    furnace = SimpleNamespace(
        is_running=True, has_data=True, has_problem=False,
        state=SimpleNamespace(remaining_seconds=3 * 3600 + 12 * 60),
        target=SimpleNamespace(name="Бочка"),
    )

    view = hp.build_place_view(works, machine_cards=[card], furnace_cards=[furnace], machine_names=MACHINES)

    counts = {item.key: item.count for item in view.strip}
    assert counts == {"p1": 2, "bk": 1, "p3": 0, "cn": 0, "mc": 1, "none": 2}
    assert view.bucket(6) == "mc", "диск на верстаті — не в печі, хоч у клітинці «1»"
    assert next(i for i in view.strip if i.key == "bk").note == "ще 3:12:00"


# ── справжній рендер ─────────────────────────────────────────────────────

DAY = date(2026, 10, 7)
TAB = DAY.strftime("%d.%m.%y")


def _seed(factory, flow):
    with factory() as db:
        user = db.scalars(select(User).where(User.username == OPERATOR[0])).one()
        user.handout_flow = flow
        for row, client, sum3d, raw in (
            (60, "Дент-Профі", "21-22-23", "бочка"),
            (61, "Лаб. Північ", "21-22-23", "бочка"),
            (62, "Сміл", "22-34-07", None),
            (63, "Дельта", "02-51-15", "RR 09:12"),
        ):
            db.add(Order(
                source="sheet_client", sheet_tab=TAB, row_number=row, client_name=client,
                material_color="mono a3", quantity="1", status="відфрезеровано",
                sum3d_id=sum3d, milled_raw=raw,
            ))
        db.commit()


def _page(app):
    client = MiniClient(app)
    status, _, _ = client.login(*OPERATOR)
    assert status in (200, 302, 303)
    status, _, html = client.get(f"/handout?source=all&day={TAB}")
    assert status == 200, html[:400]
    return client, html


@pytest.mark.parametrize("flow", ["", "sheet"])
def test_handout_renders_marks_and_strip_in_both_views(app_db, flow):  # noqa: F811
    app, factory = app_db
    _seed(factory, flow)
    _, html = _page(app)

    places = re.findall(r'class="wrow[^"]*" data-order="\d+" data-place="(\w+)"', html)
    assert sorted(places) == ["bk", "bk", "none", "none"]
    strip = dict(re.findall(
        r'data-place-filter="(\w+)"[^>]*>\s*<span class="hplace-n mono">(\d+)</span>', html
    ))
    assert strip == {"p1": "0", "bk": "2", "p3": "0", "cn": "0", "mc": "0", "none": "2"}
    assert html.count('hx-post="/handout/place/') >= 4 * 4, "меню з чотирьох печей на кожному рядку"
    assert "RR 09:12" not in html, "маркер авто-літери не показується як місце"


def test_place_route_marks_the_disc_and_writes_the_sheet(app_db, monkeypatch):  # noqa: F811
    app, factory = app_db
    _seed(factory, "")
    with factory() as db:
        engine = db.get_bind()
        target = db.scalars(select(Order).where(Order.client_name == "Сміл")).one()
        target.sum3d_id = "21-22-23"  # той самий диск, що й Дент-Профі / Лаб. Північ
        db.commit()
        target_id = target.id
    ws = _stub_sheet(monkeypatch, engine, lambda o: o.row_number + HEADER_ROWS)

    client, _ = _page(app)
    status, _, html = client.post(
        f"/handout/place/{target_id}", {"place": "p1", "source": "all", "day": TAB},
        headers={"HX-Request": "true"},
    )

    assert status == 200, html[:400]
    with factory() as db:
        marks = {o.client_name: o.milled_raw for o in db.scalars(select(Order))}
    assert marks == {"Дент-Профі": "1", "Лаб. Північ": "1", "Сміл": "1", "Дельта": "RR 09:12"}
    (updates,), _ = ws.batch_update.call_args
    assert sorted(u["range"] for u in updates) == [f"N{r + HEADER_ROWS}" for r in (60, 61, 62)]
    assert all(u["values"] == [["1"]] for u in updates)
    assert "Перша: 3 роботи диска 21-22-23" in html
    strip = dict(re.findall(
        r'data-place-filter="(\w+)"[^>]*>\s*<span class="hplace-n mono">(\d+)</span>', html
    ))
    assert strip["p1"] == "3" and strip["none"] == "1"
