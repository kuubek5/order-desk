"""POST /handout/issue-group: closes a client's handout card in one click —
every found order flips to "видано" and sheet_client rows get their blue
fill cleared in the sheet. Mocks the sheet layer (open_spreadsheet,
get_worksheet_by_name, clear_row_fills) so no real network is touched."""

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.platform_windows import _raise_explorer_window
from app.services.handout import entries_for_material
from app.stl_preview import resolve_preview_folder
from app.routers import handout as handout_router_mod
from app.services import handout as handout_service
from app.services import sheet_writeback as writeback_service
from app.db import Base
from app.models import Order, StatusEvent, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    user = User(username="root", password_hash="unused", full_name="Роман", role="адмін")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None, headers: dict | None = None):
    """`headers` — щоб перевіряти HTMX-гілку: відмітка «знайдено» з HX-Request
    віддає фрагмент списку замість редіректу."""
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(
        session=session,
        client=SimpleNamespace(host="127.0.0.1"),
        headers=headers or {},
    )


YESTERDAY = (date.today() - timedelta(days=1)).strftime("%d.%m.%y")


def _client_order(client_name="Basarab", status="знайдено при видачі", row_number=60, sheet_tab=YESTERDAY):
    return Order(
        source="sheet_client", sheet_tab=sheet_tab, row_number=row_number,
        client_name=client_name, material_color="Ti", quantity="1", status=status,
    )


def _stub_sheet(monkeypatch, sheet_id=42):
    """Fake worksheet/spreadsheet so _write_sheet_fields and clear_row_fills
    both resolve without hitting the network; captures clear_row_fills calls."""
    fake_ws = SimpleNamespace(id=sheet_id, title=YESTERDAY)
    fake_ws.acell = lambda a1: SimpleNamespace(value="")
    fake_ws.batch_update = MagicMock()
    # Відкриття таблиці бачать обидва боки: issue_group ще в web.py, а
    # write_sheet_fields уже в сервісі write-back.
    for mod in (handout_router_mod, writeback_service):
        monkeypatch.setattr(mod, "open_spreadsheet", lambda db=None: object())
        monkeypatch.setattr(mod, "get_worksheet_by_name", lambda ss, name: fake_ws)
    captured = {}

    def fake_clear(spreadsheet, rows):
        captured["rows"] = rows

    monkeypatch.setattr(handout_router_mod, "clear_row_fills", fake_clear)
    return captured


def test_requires_authentication():
    engine = _database()
    with Session(engine) as db, pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(handout_router_mod.issue_handout_group(request=_request(None), client_name="X", day="", db=db))
    assert exc.value.status_code == 401


def test_issues_nothing_when_no_work_is_marked_found(monkeypatch):
    """Жодної позначки «знайдено» → видавати нічого. Раніше тут був
    HTTPException 400; тепер це просто редірект із поясненням, бо
    натиснути кнопку без знайдених робіт оператор може лише зі
    застарілої сторінки — це не помилка, а порожня дія."""
    engine = _database()
    _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="нове"))
        db.commit()

        import asyncio
        request = _request(user.id)
        asyncio.run(
            handout_router_mod.issue_handout_group(request=request, client_name="Basarab", day="", db=db)
        )
        assert db.scalar(select(Order)).status == "нове"
        assert "handout_flash" in request.session


def test_issues_only_found_works_and_leaves_the_rest(monkeypatch):
    """ЧАСТКОВА ВИДАЧА — норма процесу (CLAUDE.md §2): цирконій іде через
    три пічки, що відкриваються в різний час, тож роботи одного клієнта
    виходять партіями. Знайдене видається зараз, решта лишається в картці."""
    engine = _database()
    captured = _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        found = _client_order(row_number=60, status="знайдено при видачі")
        waiting = _client_order(row_number=61, status="нове")
        db.add_all([found, waiting])
        db.commit()

        import asyncio
        asyncio.run(
            handout_router_mod.issue_handout_group(request=_request(user.id), client_name="Basarab", day="", db=db)
        )

        assert found.status == "видано"
        assert waiting.status == "нове"          # чекає наступної пічки
        # синю заливку знято рівно з виданого рядка, не з обох
        from app.parser import HEADER_ROWS
        assert captured["rows"] == [(42, 60 + HEADER_ROWS)]
        events = db.scalars(select(StatusEvent)).all()
        assert [e.status for e in events] == ["видано"]


def test_issues_group_and_clears_blue_fill(monkeypatch):
    engine = _database()
    captured = _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        o1 = _client_order(row_number=60)
        o2 = _client_order(row_number=61)
        db.add_all([o1, o2])
        db.commit()

        import asyncio
        resp = asyncio.run(
            handout_router_mod.issue_handout_group(request=_request(user.id), client_name="Basarab", day="", db=db)
        )
        assert resp.status_code == 303

        orders = db.scalars(select(Order)).all()
        assert all(o.status == "видано" for o in orders)
        events = db.scalars(select(StatusEvent)).all()
        assert len(events) == 2
        assert all(e.status == "видано" and e.actor == "root" for e in events)

        # both rows cleared, absolute sheet row = row_number + HEADER_ROWS
        from app.parser import HEADER_ROWS
        assert sorted(captured["rows"]) == [
            (42, 60 + HEADER_ROWS), (42, 61 + HEADER_ROWS)
        ]


def test_already_issued_orders_are_left_alone_but_still_pass(monkeypatch):
    """A group that's already fully видано (button clicked twice, or a race)
    is a no-op — no duplicate StatusEvent, no sheet write."""
    engine = _database()
    _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="видано"))
        db.commit()

        import asyncio
        resp = asyncio.run(
            handout_router_mod.issue_handout_group(request=_request(user.id), client_name="Basarab", day="", db=db)
        )
        assert resp.status_code == 303
        # order already excluded from candidates (status != "видано" filter)
        assert db.scalar(select(StatusEvent)) is None


def test_lab_orders_in_group_are_not_sent_to_clear_row_fills(monkeypatch):
    """A "lab" order (наряд, not a наряд-less client row) has no blue fill to
    clear — only sheet_client rows go into the clear batch."""
    engine = _database()
    captured = _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        lab_order = Order(
            source="lab", sheet_tab=YESTERDAY, row_number=7, work_order_no="24999",
            client_name="Basarab", material_color="Ti", quantity="1",
            status="знайдено при видачі",
        )
        db.add(lab_order)
        db.commit()

        import asyncio
        asyncio.run(
            handout_router_mod.issue_handout_group(request=_request(user.id), client_name="Basarab", day="", db=db)
        )
        assert "rows" not in captured or captured["rows"] == []


def test_sheet_failure_still_marks_issued_and_flashes_error(monkeypatch):
    """A Sheets write hiccup must never block the видано status — the DB is
    the source of truth and the operator gets a visible warning instead."""
    engine = _database()
    _stub_sheet(monkeypatch)

    def boom(spreadsheet, rows):
        raise RuntimeError("проксі впав")

    monkeypatch.setattr(handout_router_mod, "clear_row_fills", boom)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order())
        db.commit()

        request = _request(user.id)
        import asyncio
        resp = asyncio.run(handout_router_mod.issue_handout_group(request=request, client_name="Basarab", day="", db=db))
        assert resp.status_code == 303
        assert db.scalar(select(Order)).status == "видано"
        assert request.session["handout_flash"]["kind"] == "error"


def test_group_disappears_from_handout_listing_after_issue(monkeypatch):
    engine = _database()
    _stub_sheet(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order())
        db.commit()

        import asyncio
        asyncio.run(
            handout_router_mod.issue_handout_group(request=_request(user.id), client_name="Basarab", day="", db=db)
        )

        with patch.object(web, "list_export_client_names_cached", return_value=[]):
            ctx_holder = {}
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: ctx_holder.update(context) or context,
            )
            handout_router_mod.get_handout(request=_request(user.id), db=db)
        assert ctx_holder["client_groups"] == []


def _get_handout_context(monkeypatch, db, user_id, day="all"):
    """Порожній `day` тепер означає «останній день», а не «всі» — тому тести,
    яким потрібен увесь список, просять його явно."""
    with patch.object(web, "list_export_client_names_cached", return_value=[]):
        ctx_holder = {}
        monkeypatch.setattr(
            web.templates, "TemplateResponse",
            lambda request, template, context: ctx_holder.update(context) or context,
        )
        handout_router_mod.get_handout(request=_request(user_id), day=day, db=db)
    return ctx_holder


def test_handout_orders_follow_sheet_top_to_bottom_order(monkeypatch):
    """One client, three works — furnaces close at different times through the
    day, so the sheet's own row order is a rough readiness timeline; the
    handout card must show them in that order, not DB insertion order."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        # inserted out of row order on purpose
        db.add(_client_order(client_name="Basarab", status="нове", row_number=90))
        db.add(_client_order(client_name="Basarab", status="нове", row_number=61))
        db.add(_client_order(client_name="Basarab", status="нове", row_number=75))
        db.commit()

        ctx = _get_handout_context(monkeypatch, db, user.id)
        row_numbers = [o.row_number for o in ctx["client_groups"][0]["orders"]]
        assert row_numbers == [61, 75, 90]


def test_handout_cards_follow_sheet_order_by_earliest_work(monkeypatch):
    """Card order itself follows each client's earliest work position, so
    flipping through the handout screen mirrors flipping through the table."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(client_name="Later", status="нове", row_number=90))
        db.add(_client_order(client_name="Earlier", status="нове", row_number=61))
        db.commit()

        ctx = _get_handout_context(monkeypatch, db, user.id)
        names = [g["client_name"] for g in ctx["client_groups"]]
        assert names == ["Earlier", "Later"]


def test_handout_orders_older_day_sorts_before_newer_day(monkeypatch):
    engine = _database()
    two_days_ago = (date.today() - timedelta(days=2)).strftime("%d.%m.%y")
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(client_name="Basarab", status="нове", row_number=5, sheet_tab=YESTERDAY))
        db.add(_client_order(client_name="Basarab", status="нове", row_number=200, sheet_tab=two_days_ago))
        db.commit()

        ctx = _get_handout_context(monkeypatch, db, user.id)
        tabs = [o.sheet_tab for o in ctx["client_groups"][0]["orders"]]
        assert tabs == [two_days_ago, YESTERDAY]  # older day first even though row 200 > row 5


def test_handout_day_chips_list_days_with_unissued_works(monkeypatch):
    engine = _database()
    two_days_ago = (date.today() - timedelta(days=2)).strftime("%d.%m.%y")
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(client_name="A", status="нове", sheet_tab=YESTERDAY))
        db.add(_client_order(client_name="B", status="нове", sheet_tab=two_days_ago))
        db.commit()

        ctx = _get_handout_context(monkeypatch, db, user.id, day="all")
        assert ctx["handout_days"] == [two_days_ago, YESTERDAY]
        assert ctx["selected_day"] == ""


def test_handout_day_filter_narrows_to_that_day(monkeypatch):
    """?day=14.08.26 shows only that day's works — a client whose works span
    days shows a day-sized card, and a client with nothing that day drops."""
    engine = _database()
    two_days_ago = (date.today() - timedelta(days=2)).strftime("%d.%m.%y")
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(client_name="Both", status="нове", row_number=60, sheet_tab=YESTERDAY))
        db.add(_client_order(client_name="Both", status="нове", row_number=61, sheet_tab=two_days_ago))
        db.add(_client_order(client_name="OnlyOld", status="нове", sheet_tab=two_days_ago))
        db.commit()

        with patch.object(web, "list_export_client_names_cached", return_value=[]):
            ctx_holder = {}
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: ctx_holder.update(context) or context,
            )
            handout_router_mod.get_handout(request=_request(user.id), day=YESTERDAY, db=db)

        assert ctx_holder["selected_day"] == YESTERDAY
        names = [g["client_name"] for g in ctx_holder["client_groups"]]
        assert names == ["Both"]
        assert [o.sheet_tab for o in ctx_holder["client_groups"][0]["orders"]] == [YESTERDAY]


def test_issue_group_with_day_only_closes_that_day(monkeypatch):
    """"Видати" on a day-filtered card closes only that day's works — the
    client's other-day works stay open even if they're also found."""
    engine = _database()
    captured = _stub_sheet(monkeypatch)
    two_days_ago = (date.today() - timedelta(days=2)).strftime("%d.%m.%y")
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        o_new = _client_order(status="знайдено при видачі", row_number=60, sheet_tab=YESTERDAY)
        o_old = _client_order(status="знайдено при видачі", row_number=61, sheet_tab=two_days_ago)
        db.add_all([o_new, o_old])
        db.commit()

        import asyncio
        resp = asyncio.run(
            handout_router_mod.issue_handout_group(
                request=_request(user.id), client_name="Basarab", day=YESTERDAY, db=db
            )
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/handout?day={YESTERDAY}"

        statuses = {o.sheet_tab: o.status for o in db.scalars(select(Order))}
        assert statuses[YESTERDAY] == "видано"
        assert statuses[two_days_ago] == "знайдено при видачі"  # untouched
        from app.parser import HEADER_ROWS
        assert captured["rows"] == [(42, 60 + HEADER_ROWS)]


def _stub_fills(monkeypatch, sheet_id=42):
    """Fake sheet layer for mark-found/unmark-found: captures which of
    clear_row_fills (blue→white) / paint_row_fills (white→blue) got which rows."""
    fake_ws = SimpleNamespace(id=sheet_id, title=YESTERDAY)
    monkeypatch.setattr(writeback_service, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(writeback_service, "get_worksheet_by_name", lambda ss, name: fake_ws)
    captured = {}
    monkeypatch.setattr(
        writeback_service, "clear_row_fills", lambda ss, rows: captured.__setitem__("clear", rows)
    )
    monkeypatch.setattr(
        writeback_service, "paint_row_fills", lambda ss, rows: captured.__setitem__("paint", rows)
    )
    return captured


def _inline_fill_background(monkeypatch, db):
    """Запис заливки тепер іде у фоновий воркер із ВЛАСНОЮ сесією — у тестах
    це вело б у справжню SessionLocal. Виконуємо його одразу й на тестовій
    сесії: перевіряємо, що фарбують правильно, не втрачаючи асинхронності
    самого обробника (для неї є окремий тест нижче)."""
    monkeypatch.setattr(
        handout_router_mod,
        "set_client_row_fill_background",
        lambda order_id, *, blue: writeback_service.set_client_row_fill(
            db, db.get(Order, order_id), blue=blue
        ),
    )


def test_mark_found_clears_blue_and_keeps_day(monkeypatch):
    """The "знайдено" checkbox flips one order to "знайдено при видачі",
    clears its blue fill to white, and returns to the SAME day-filtered view
    (the old bug bounced the operator to the unfiltered all-days list)."""
    engine = _database()
    captured = _stub_fills(monkeypatch)
    from app.parser import HEADER_ROWS
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="нове", row_number=60, sheet_tab=YESTERDAY))
        db.commit()
        order = db.scalar(select(Order))
        _inline_fill_background(monkeypatch, db)

        import asyncio
        resp = asyncio.run(
            handout_router_mod.mark_found(
                request=_request(user.id), order_id=order.id,
                source="all", day=YESTERDAY, db=db,
            )
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/handout?day={YESTERDAY}"
        db.refresh(order)
        assert order.status == "знайдено при видачі"
        assert captured["clear"] == [(42, 60 + HEADER_ROWS)]
        assert "paint" not in captured


def test_unmark_found_reverts_and_repaints_blue(monkeypatch):
    """Зняття випадкової галочки повертає ПОПЕРЕДНІЙ статус і перефарбовує
    рядок таблиці в синій.

    Раніше тут стояло «нове», і це було помилкою: робота на видачі за
    визначенням уже відфрезерована, тож випадковий клік і назад показував її
    невиготовленою — рівно той стан, який §5 називає «записалась, а не
    зробилась». Без історії статусів відкочуємось у «відфрезеровано»."""
    engine = _database()
    captured = _stub_fills(monkeypatch)
    from app.parser import HEADER_ROWS
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="знайдено при видачі", row_number=60, sheet_tab=YESTERDAY))
        db.commit()
        order = db.scalar(select(Order))
        _inline_fill_background(monkeypatch, db)

        import asyncio
        resp = asyncio.run(
            handout_router_mod.unmark_found(
                request=_request(user.id), order_id=order.id,
                source="email", day=YESTERDAY, db=db,
            )
        )
        assert resp.status_code == 303
        # source is preserved alongside the day
        assert resp.headers["location"] == f"/handout?source=email&day={YESTERDAY}"
        db.refresh(order)
        assert order.status == "відфрезеровано"
        assert captured["paint"] == [(42, 60 + HEADER_ROWS)]
        assert "clear" not in captured


def test_unmark_found_restores_the_real_previous_status(monkeypatch):
    """Коли історія статусів є — повертаємо саме її останній запис, а не
    здогадку. Інакше зняття галочки стирало б реальний стан роботи."""
    engine = _database()
    _stub_fills(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="знайдено при видачі", row_number=61, sheet_tab=YESTERDAY))
        db.commit()
        order = db.scalar(select(Order))
        db.add(StatusEvent(order_id=order.id, operator_id=user.id,
                           status="переробка", actor=user.username))
        db.add(StatusEvent(order_id=order.id, operator_id=user.id,
                           status="знайдено при видачі", actor=user.username))
        db.commit()
        _inline_fill_background(monkeypatch, db)

        import asyncio
        asyncio.run(
            handout_router_mod.unmark_found(
                request=_request(user.id), order_id=order.id,
                source="all", day=YESTERDAY, db=db,
            )
        )
        db.refresh(order)
        assert order.status == "переробка"


def test_unmark_found_ignores_already_issued(monkeypatch):
    """Un-mark refuses to touch an already-issued ("видано") order — no status
    change, no sheet repaint."""
    engine = _database()
    captured = _stub_fills(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(status="видано", row_number=60, sheet_tab=YESTERDAY))
        db.commit()
        order = db.scalar(select(Order))

        import asyncio
        resp = asyncio.run(
            handout_router_mod.unmark_found(
                request=_request(user.id), order_id=order.id,
                source="all", day="", db=db,
            )
        )
        assert resp.status_code == 303
        db.refresh(order)
        assert order.status == "видано"
        assert captured == {}  # no fill call at all


def test_mark_found_lab_order_skips_sheet_fill(monkeypatch):
    """A lab order (not sheet_client) was never blue — marking it found flips
    status but touches no fill in the sheet."""
    engine = _database()
    captured = _stub_fills(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(Order(
            source="lab", sheet_tab=YESTERDAY, row_number=10,
            client_name="Basarab", work_order_no="24122", status="відфрезеровано",
        ))
        db.commit()
        order = db.scalar(select(Order))

        import asyncio
        resp = asyncio.run(
            handout_router_mod.mark_found(
                request=_request(user.id), order_id=order.id,
                source="all", day=YESTERDAY, db=db,
            )
        )
        assert resp.status_code == 303
        db.refresh(order)
        assert order.status == "знайдено при видачі"
        assert captured == {}  # no clear, no paint for a lab row


class TestMaterialMatching:
    """Per-row export-folder matching (get_handout): narrows a client's folders
    to the ones whose material matches a given work, an assist not an exact bind
    (the path carries no наряд/Sum3D ID)."""

    # Самі правила збігу — в tests/test_material_match.py; тут лише те, що
    # робить із ними екран: відбір і порядок.

    def test_entries_for_material_filters_and_sorts_by_time(self):
        from datetime import datetime
        
        e1 = SimpleNamespace(material_color_folder_name="emo a3", created_at=datetime(2026, 8, 14, 11, 0))
        e2 = SimpleNamespace(material_color_folder_name="emo a35", created_at=datetime(2026, 8, 14, 9, 0))
        e3 = SimpleNamespace(material_color_folder_name="emo a3", created_at=datetime(2026, 8, 14, 8, 0))
        got = entries_for_material("emo a3", [e1, e2, e3])
        assert got == [e3, e1]  # only emo a3, oldest-first

    def test_entries_for_material_empty_when_no_colour(self):
        
        e1 = SimpleNamespace(material_color_folder_name="emo a3", created_at=None)
        assert entries_for_material("", [e1]) == []
        assert entries_for_material(None, [e1]) == []


class TestHandoutDayWindow:
    """The pager shows a few neighbouring days, not one date and not a wall of
    30+ chips: the operator hands out one day at a time but needs to see where
    that day sits and reach the one before it in a single click."""

    @staticmethod
    def _days(*day_numbers):
        from datetime import date
        return [date(2026, 8, d) for d in day_numbers]

    def _labels(self, days, selected):
        return [d["label"] for d in handout_router_mod.handout_day_window(days, selected)]

    def test_window_centres_on_the_selected_day(self):
        days = self._days(20, 21, 22, 23, 24)
        window = handout_router_mod.handout_day_window(days, days[2])
        assert [d["label"] for d in window] == ["21.08", "22.08", "23.08"]
        assert [d["active"] for d in window] == [False, True, False]

    def test_window_stays_full_at_the_newest_end(self):
        days = self._days(20, 21, 22, 23, 24)
        assert self._labels(days, days[-1]) == ["22.08", "23.08", "24.08"]

    def test_window_stays_full_at_the_oldest_end(self):
        days = self._days(20, 21, 22, 23, 24)
        assert self._labels(days, days[0]) == ["20.08", "21.08", "22.08"]

    def test_no_selection_anchors_on_the_newest_days(self):
        days = self._days(20, 21, 22, 23, 24)
        window = handout_router_mod.handout_day_window(days, None)
        assert [d["label"] for d in window] == ["22.08", "23.08", "24.08"]
        assert not any(d["active"] for d in window)   # «усі дні» is the active state

    def test_shorter_history_than_the_window_is_not_padded(self):
        days = self._days(24, 25)
        assert self._labels(days, days[0]) == ["24.08", "25.08"]

    def test_no_days_gives_an_empty_pager(self):
        assert handout_router_mod.handout_day_window([], None) == []

    def test_value_keeps_the_full_year_for_the_query_string(self):
        days = self._days(24)
        entry = handout_router_mod.handout_day_window(days, days[0])[0]
        assert entry["label"] == "24.08" and entry["value"] == "24.08.26"


def test_handout_reads_only_the_clients_on_screen(monkeypatch, tmp_path):
    """Регрес-гард для «GET /handout took 65.281s» (бойовий лог 25.08.26).

    `export` — шара Synology через SMB, де ціну диктує КІЛЬКІСТЬ звернень.
    Сторінка обходила все дерево, хоча показує 10-20 клієнтів із сотень.
    Тепер глибина сховища читається тільки для тих, хто реально на екрані."""
    engine = _database()
    _stub_sheet(monkeypatch)

    # у сховищі 100 тек, на екрані буде один клієнт
    root = tmp_path / "export"
    for i in range(100):
        (root / f"Клієнт {i}" / "17.08.26" / "mono a3").mkdir(parents=True)
    (root / "Basarab" / "17.08.26" / "Ti").mkdir(parents=True)

    monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(root))
    from app import export_scanner
    export_scanner.clear_export_cache()

    deep_reads = []
    real_deep = export_scanner.scan_export_client

    def counting_deep(r, name, not_before=None):
        deep_reads.append(name)
        return real_deep(r, name, not_before)

    monkeypatch.setattr(handout_service, "scan_export_client_cached", counting_deep)
    monkeypatch.setattr(
        web, "list_export_client_names_cached", export_scanner.list_export_client_names
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(_client_order(client_name="Basarab", status="знайдено при видачі"))
        db.commit()
        with patch.object(web.templates, "TemplateResponse", return_value="ok"):
            handout_router_mod.get_handout(request=_request(user.id), source="all", day="", db=db)

    assert deep_reads == ["Basarab"], (
        f"глибина сховища мала читатись лише для клієнта на екрані, "
        f"а прочитано {len(deep_reads)} тек: {deep_reads[:5]}"
    )


class TestExportPrewarm:
    """Фоновий прогрів кешу обходу export.

    Обхід сховища коштує стільки, скільки коштує (виміряно на бойовій шарі
    27.08.26: 33 мс/запис послідовно, ~2525 записів на екрані). Питання —
    хто платить: оператор, який відкрив видачу, чи фон. Прогрів має сенс
    ЛИШЕ якщо він звертається до сховища з тими самими аргументами, що й
    сам екран, — інакше наповнить кеш під іншим ключем і оператор однаково
    чекатиме. Саме це тут і перевіряється."""

    def _seed(self, db):
        _user(db)
        db.add(_client_order(client_name="Basarab", status="прийнято"))
        db.add(_client_order(client_name="Кривовид", status="прийнято", row_number=61))
        db.commit()

    def _record_scans(self, monkeypatch):
        calls = []
        # Екран уже в роутері, прогрів ще у web — саме тому тест і патчить
        # обидва: він перевіряє, що вони звертаються до сховища однаково.
        for mod in (web, handout_router_mod):
            monkeypatch.setattr(
                mod, "list_export_client_names_cached", lambda root: ["Basarab", "Кривовид"]
            )
            monkeypatch.setattr(mod, "get_export_folder_path", lambda db: "Z:\\")
        monkeypatch.setattr(
            handout_service,
            "scan_export_client_cached",
            lambda root, folder, not_before: calls.append((str(root), folder, not_before)) or [],
        )
        return calls

    def test_prewarm_hits_the_same_cache_keys_as_the_screen(self, monkeypatch):
        engine = _database()
        with Session(engine) as db:
            self._seed(db)
            user_id = db.scalar(select(User.id))

            screen_calls = self._record_scans(monkeypatch)
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: context,
            )
            handout_router_mod.get_handout(request=_request(user_id), db=db)
            from_screen = set(screen_calls)

            warm_calls = self._record_scans(monkeypatch)
            web.export_warm_once(db)
            from_warm = set(warm_calls)

        assert from_screen, "екран мусить звертатися до сховища — інакше тест ні про що"
        assert from_warm == from_screen

    def test_prewarm_skips_an_unreachable_export_root(self, monkeypatch):
        engine = _database()
        with Session(engine) as db:
            self._seed(db)
            monkeypatch.setattr(web, "get_export_folder_path", lambda db: "Z:\\")
            monkeypatch.setattr(web, "list_export_client_names_cached", lambda root: [])
            assert web.export_warm_once(db) == 0


class TestHandoutDefaultDay:
    """Порожній `day` відкриває НАЙНОВІШИЙ день, а не всі 30 одразу.

    Не косметика, а швидкодія: усі дні разом давали 262 клієнти на екрані й
    ~2525 тек, які треба обійти на мережевому сховищі ще до першого рядка
    HTML (виміряно 27.08.26 — 33 мс на теку). Один день — це десятки тек.
    Умова, за якою це чесно: хвіст не зникає мовчки, і повернутись до всіх
    днів можна одним кліком."""

    def _two_days(self, db):
        two_days_ago = (date.today() - timedelta(days=2)).strftime("%d.%m.%y")
        db.add(_client_order(client_name="A", status="нове", sheet_tab=YESTERDAY))
        db.add(_client_order(client_name="B", status="нове", row_number=61, sheet_tab=two_days_ago))
        db.add(_client_order(client_name="C", status="нове", row_number=62, sheet_tab=two_days_ago))
        db.commit()
        return two_days_ago

    def test_empty_day_opens_the_newest_day_only(self, monkeypatch):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            self._two_days(db)
            ctx = _get_handout_context(monkeypatch, db, user.id, day="")
        assert ctx["selected_day"] == YESTERDAY
        assert [g["client_name"] for g in ctx["client_groups"]] == ["A"]

    def test_the_hidden_tail_is_counted_not_swallowed(self, monkeypatch):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            self._two_days(db)
            ctx = _get_handout_context(monkeypatch, db, user.id, day="")
        assert ctx["other_days_count"] == 2

    def test_all_days_stays_reachable(self, monkeypatch):
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            self._two_days(db)
            ctx = _get_handout_context(monkeypatch, db, user.id, day="all")
        assert ctx["selected_day"] == ""
        assert ctx["other_days_count"] == 0
        assert len(ctx["client_groups"]) == 3

    def test_links_and_forms_carry_the_all_days_choice(self, monkeypatch):
        """`day_param` — те, що шаблон кладе в посилання й форми. Порожній
        рядок означав би «замовчування», тобто клік по вкладці джерела
        мовчки викидав би оператора з «усіх днів» на останній."""
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            self._two_days(db)
            all_days = _get_handout_context(monkeypatch, db, user.id, day="all")
            one_day = _get_handout_context(monkeypatch, db, user.id, day="")
        assert all_days["day_param"] == "all"
        assert one_day["day_param"] == YESTERDAY

    def test_a_broken_day_link_falls_back_to_the_default(self, monkeypatch):
        """Зіпсоване посилання не має відкривати найважчий можливий екран."""
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            self._two_days(db)
            ctx = _get_handout_context(monkeypatch, db, user.id, day="не-дата")
        assert ctx["selected_day"] == YESTERDAY


class TestClientFolderOpens:
    """Кнопка «Відкрити папку» на картці клієнта.

    Вона лишається `<a href="file://...">`, але браузер МОВЧКИ блокує перехід
    на file:// зі сторінки на http — саме тому кнопка не робила нічого
    (бойовий випадок 28.08.26, клієнт Pavlenko: тека прив'язана, лежить на
    диску, кнопка мертва). Провідник тепер відкриває сервер за токеном, тож
    токен мусить бути в контексті — інакше JS нема чого надіслати."""

    def test_the_button_carries_a_token_the_server_can_resolve(self, monkeypatch, tmp_path):
        from datetime import datetime

        client_folder = tmp_path / "Pavlenko"
        material_folder = client_folder / "26.08.26" / "Emotions A3 опаковий всередині"
        material_folder.mkdir(parents=True)

        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_client_order(client_name="Pavlenko", status="нове"))
            db.commit()

            entry = SimpleNamespace(
                client_folder_name="Pavlenko",
                batch_folder_name="26.08.26",
                material_color_folder_name="Emotions A3 опаковий всередині",
                created_at=datetime(2026, 8, 26, 10, 16),
                files=["a.stl"],
                folder_path=material_folder,
            )
            monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
            # Токен резолвиться НЕ через web.get_export_folder_path, а через
            # власну мапу коренів stl_preview — саме вона тримає перевірку
            # «шлях справді всередині довіреного кореня».
            import app.stl_preview as stl_preview
            monkeypatch.setitem(
                stl_preview._ROOT_RESOLVERS, "export", lambda db: str(tmp_path)
            )
            monkeypatch.setattr(
                web, "list_export_client_names_cached", lambda root: ["Pavlenko"]
            )
            monkeypatch.setattr(
                handout_service, "scan_export_client_cached", lambda root, folder, nb: [entry]
            )
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: context,
            )
            ctx = handout_router_mod.get_handout(request=_request(user.id), day="", db=db)

            group = ctx["client_groups"][0]
            assert group["client_folder_token"], "без токена кнопка знову буде мертвою"
            assert resolve_preview_folder(db, group["client_folder_token"]) == client_folder

    def test_the_matching_folder_reaches_the_row(self, monkeypatch, tmp_path):
        """Той самий випадок з іншого боку: `emo a3` у таблиці мусить знайти
        теку `Emotions A3 опаковий всередині`."""
        from datetime import datetime

        material_folder = tmp_path / "Pavlenko" / "26.08.26" / "Emotions A3 опаковий всередині"
        material_folder.mkdir(parents=True)

        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            order = _client_order(client_name="Pavlenko", status="нове")
            order.material_color = "emo a3"
            db.add(order)
            db.commit()

            entry = SimpleNamespace(
                client_folder_name="Pavlenko",
                batch_folder_name="26.08.26",
                material_color_folder_name="Emotions A3 опаковий всередині",
                created_at=datetime(2026, 8, 26, 10, 16),
                files=["a.stl"],
                folder_path=material_folder,
            )
            monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
            monkeypatch.setattr(
                web, "list_export_client_names_cached", lambda root: ["Pavlenko"]
            )
            monkeypatch.setattr(
                handout_service, "scan_export_client_cached", lambda root, folder, nb: [entry]
            )
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: context,
            )
            ctx = handout_router_mod.get_handout(request=_request(user.id), day="", db=db)

        matched = ctx["client_groups"][0]["orders"][0].export_matches
        assert [e.material_color_folder_name for e in matched] == [
            "Emotions A3 опаковий всередині"
        ]


class TestOneBatchPerRow:
    """Під рядком роботи мають бути теки ОДНІЄЇ партії, не всі за тиждень.

    Скриншот 28.08.26: у клієнта Кривовид одна робота `mono a3.5`, а під нею
    чотири теки. Причина — постійний клієнт замовляє той самий матеріал мало
    не щодня, а відбір брав усе вікно сканування. Прив'язки «рядок ↔ тека» в
    шляху немає (CLAUDE.md §4), але робота не могла лежати в партії, скачаній
    пізніше за неї, — цим і звужуємо."""

    def _entry(self, name, when):
        from datetime import datetime
        return SimpleNamespace(material_color_folder_name=name, created_at=when)

    def test_only_the_nearest_earlier_batch_survives(self):
        from datetime import date, datetime
        

        entries = [
            self._entry("mono a3.5", datetime(2026, 8, 21, 8, 59)),
            self._entry("mono a3.5", datetime(2026, 8, 24, 9, 30)),
            self._entry("mono a3.5", datetime(2026, 8, 27, 8, 5)),
        ]
        picked = entries_for_material("mono a3.5", entries, date(2026, 8, 27))
        assert [e.created_at.day for e in picked] == [27]

    def test_several_folders_of_the_SAME_day_all_stay(self):
        """Дві теки одного дня — це справді неоднозначність, яку вирішує око
        оператора; ховати одну з них не можна."""
        from datetime import date, datetime
        

        entries = [
            self._entry("mono a3.5", datetime(2026, 8, 27, 8, 5)),
            self._entry("mono a3.5", datetime(2026, 8, 27, 12, 15)),
            self._entry("mono a3.5", datetime(2026, 8, 24, 9, 30)),
        ]
        picked = entries_for_material("mono a3.5", entries, date(2026, 8, 27))
        assert [e.created_at.hour for e in picked] == [8, 12]

    def test_files_uploaded_the_next_day_are_not_lost(self):
        """Партії раніше за роботу немає — беремо найранішу пізнішу, інакше
        рядок лишився б зовсім без теки."""
        from datetime import date, datetime
        

        entries = [
            self._entry("mono a3.5", datetime(2026, 8, 28, 9, 0)),
            self._entry("mono a3.5", datetime(2026, 8, 29, 9, 0)),
        ]
        picked = entries_for_material("mono a3.5", entries, date(2026, 8, 27))
        assert [e.created_at.day for e in picked] == [28]

    def test_without_a_work_day_nothing_is_narrowed(self):
        """Робота без дати вкладки не має на що спиратись — краще показати
        все, ніж навмання відрізати."""
        from datetime import datetime
        

        entries = [
            self._entry("mono a3.5", datetime(2026, 8, 21, 8, 59)),
            self._entry("mono a3.5", datetime(2026, 8, 27, 8, 5)),
        ]
        assert len(entries_for_material("mono a3.5", entries, None)) == 2


class TestRaiseExplorerWindow:
    """Підняття вікна Провідника — best-effort і НЕ має нічого ламати.

    Саму поведінку (згорнуте вікно → розгорнуте й активне) перевірено наживо
    28.08.26 на Windows: ці тести лише стережуть, щоб помічник мовчки
    здавався замість того, щоб валити відкриття теки, яке вже відбулось."""

    def test_a_missing_window_just_times_out_quietly(self):
        import time as _time

        started = _time.monotonic()
        _raise_explorer_window(Path("Тека-якої-точно-немає"), timeout=0.05)
        assert _time.monotonic() - started < 2

    def test_an_empty_name_is_not_searched_for(self):
        # Порожня ціль збіглася б із будь-яким вікном без заголовка.
        _raise_explorer_window(Path("   "), timeout=0.05)


class TestMarkFoundIsInstant:
    """Галочка «знайдено» не має чекати на Google Таблицю.

    Скарга оператора 28.08.26: «довго думає, довго ставить галочку». Запис
    заливки йшов синхронно і на потоці ЗАПИТУ — тобто повз теплий кеш
    write-back воркера, платячи за відкриття таблиці заново. На видачі
    галочки клацають підряд, тож ця затримка діставалась десятки разів за
    ранок."""

    def test_the_request_never_touches_the_sheet_itself(self, monkeypatch):
        engine = _database()
        touched = []
        monkeypatch.setattr(
            writeback_service, "set_client_row_fill",
            lambda db, order, *, blue: touched.append(blue) or None,
        )
        queued = []
        monkeypatch.setattr(
            handout_router_mod, "set_client_row_fill_background",
            lambda order_id, *, blue: queued.append((order_id, blue)),
        )
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_client_order(status="нове", row_number=60, sheet_tab=YESTERDAY))
            db.commit()
            order = db.scalar(select(Order))

            import asyncio
            asyncio.run(
                handout_router_mod.mark_found(
                    request=_request(user.id), order_id=order.id,
                    source="all", day=YESTERDAY, db=db,
                )
            )
            assert queued == [(order.id, False)]
        assert touched == [], "таблицю чіпає фон, а не обробник кліку"

    def test_the_status_is_committed_before_the_sheet_is_queued(self, monkeypatch):
        """Фон читає роботу власною сесією — якщо поставити його в чергу до
        коміту, він побачить стару заливку або взагалі нічого."""
        engine = _database()
        seen_status = {}
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_client_order(status="нове", row_number=60, sheet_tab=YESTERDAY))
            db.commit()
            order = db.scalar(select(Order))
            monkeypatch.setattr(
                handout_router_mod, "set_client_row_fill_background",
                lambda order_id, *, blue: seen_status.update(
                    dirty=bool(db.dirty or db.new)
                ),
            )

            import asyncio
            asyncio.run(
                handout_router_mod.mark_found(
                    request=_request(user.id), order_id=order.id,
                    source="all", day=YESTERDAY, db=db,
                )
            )
        assert seen_status == {"dirty": False}


class TestScrollStaysPut:
    """Відмітка «знайдено» не має перезавантажувати сторінку.

    Скарга оператора 28.08.26: «екран смикається наверх». Кожна галочка йшла
    звичайною формою з редіректом на /handout, тобто повним перезавантаженням
    — а оператор іде списком згори вниз і клацає їх підряд. Тепер HTMX
    підмінює лише список карток.

    Шаблони тут рендеряться ПО-СПРАВЖНЬОМУ (без підміни TemplateResponse):
    помилка в партіалі інакше знайшлась би вже в оператора."""

    def _screen(self, monkeypatch, tmp_path):
        monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
        monkeypatch.setattr(handout_router_mod, "list_export_client_names_cached", lambda root: [])

    def test_an_htmx_mark_returns_the_card_list_not_a_redirect(self, monkeypatch, tmp_path):
        engine = _database()
        self._screen(monkeypatch, tmp_path)
        monkeypatch.setattr(handout_router_mod, "set_client_row_fill_background", lambda order_id, *, blue: None)
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_client_order(client_name="Basarab", status="нове", sheet_tab=YESTERDAY))
            db.commit()
            order = db.scalar(select(Order))

            import asyncio
            resp = asyncio.run(
                handout_router_mod.mark_found(
                    request=_request(user.id, headers={"HX-Request": "true"}),
                    order_id=order.id, source="all", day=YESTERDAY, db=db,
                )
            )
            assert resp.status_code == 200
            body = resp.body.decode("utf-8")
            assert 'id="handout-list"' in body
            # Це ФРАГМЕНТ, не сторінка — інакше HTMX вставив би сторінку в себе.
            assert "<html" not in body.lower()

    def test_a_plain_form_still_redirects(self, monkeypatch, tmp_path):
        """Без JS форма мусить працювати по-старому."""
        engine = _database()
        self._screen(monkeypatch, tmp_path)
        monkeypatch.setattr(handout_router_mod, "set_client_row_fill_background", lambda order_id, *, blue: None)
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_client_order(client_name="Basarab", status="нове", sheet_tab=YESTERDAY))
            db.commit()
            order = db.scalar(select(Order))

            import asyncio
            resp = asyncio.run(
                handout_router_mod.mark_found(
                    request=_request(user.id), order_id=order.id,
                    source="all", day=YESTERDAY, db=db,
                )
            )
        assert resp.status_code == 303

    def test_the_full_page_still_renders_with_the_partial(self, monkeypatch, tmp_path):
        engine = _database()
        self._screen(monkeypatch, tmp_path)
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_client_order(client_name="Basarab", status="нове", sheet_tab=YESTERDAY))
            db.commit()
            resp = handout_router_mod.get_handout(request=_request(user.id), day="", db=db)
        body = resp.body.decode("utf-8")
        assert 'id="handout-list"' in body
        assert "Basarab" in body


class TestBoundClientWithoutFreshBatches:
    """Тека прив'язана, але свіжих партій немає.

    Бойовий випадок 28.08.26 (Светлана Криничко): у «Клієнтах» стоїть
    «папку прив'язано», а видача пропонує «Прив'язати папку» й не показує
    жодної теки. Причина — посилання на теку клієнта бралося з ЗНАЙДЕНИХ
    ПАРТІЙ, тож «немає свіжих партій» виглядало як «не прив'язано». Це два
    різні стани, і плутати їх не можна: у першому оператор шукає теку, якої
    насправді не бракує."""

    def test_the_folder_link_survives_an_empty_window(self, monkeypatch, tmp_path):
        (tmp_path / "Светлана  Криничко").mkdir()
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            db.add(_client_order(client_name="Светлана Криничко", status="нове"))
            db.commit()

            monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
            monkeypatch.setattr(
                web, "list_export_client_names_cached", lambda root: ["Светлана  Криничко"]
            )
            # Вікно за датою не дало нічого, і запасний шлях теж порожній —
            # тека є, партій немає.
            monkeypatch.setattr(handout_service, "scan_export_client_cached", lambda root, f, nb: [])
            monkeypatch.setattr(handout_service, "scan_export_client_latest_cached", lambda root, f: [])
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: context,
            )
            ctx = handout_router_mod.get_handout(request=_request(user.id), day="", db=db)

        group = ctx["client_groups"][0]
        assert group["client_folder_uri"], "тека прив'язана — посилання мусить бути"
        assert ctx["unbound_count"] == 0

    def test_the_latest_batches_are_used_when_the_window_is_empty(self, monkeypatch, tmp_path):
        """Файли скачали задовго до фрезерування — робота однаково має знайти
        свою теку, а не лишитись ні з чим."""
        from datetime import datetime

        material = tmp_path / "Клієнт" / "01.07.26" / "pmma kappa"
        material.mkdir(parents=True)
        entry = SimpleNamespace(
            client_folder_name="Клієнт",
            batch_folder_name="01.07.26",
            material_color_folder_name="pmma kappa",
            created_at=datetime(2026, 7, 1, 10, 0),
            files=["a.stl"],
            folder_path=material,
        )
        engine = _database()
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)
            order = _client_order(client_name="Клієнт", status="нове")
            order.material_color = "pmma kappa"
            db.add(order)
            db.commit()

            monkeypatch.setattr(handout_router_mod, "get_export_folder_path", lambda db: str(tmp_path))
            monkeypatch.setattr(
                web, "list_export_client_names_cached", lambda root: ["Клієнт"]
            )
            monkeypatch.setattr(handout_service, "scan_export_client_cached", lambda root, f, nb: [])
            monkeypatch.setattr(
                handout_service, "scan_export_client_latest_cached", lambda root, f: [entry]
            )
            monkeypatch.setattr(
                web.templates, "TemplateResponse",
                lambda request, template, context: context,
            )
            ctx = handout_router_mod.get_handout(request=_request(user.id), day="", db=db)

        matched = ctx["client_groups"][0]["orders"][0].export_matches
        assert [e.batch_folder_name for e in matched] == ["01.07.26"]
