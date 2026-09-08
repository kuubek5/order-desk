from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.business_day import business_today
from app.services.queue import RETENTION_DAYS, order_is_archived
from app.routers import orders as orders_router_mod
from app.routers import archive as archive_router_mod
from app.db import Base
from app.models import Order, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Operator")
    db.add(user)
    db.commit()
    return user


def _request(user_id):
    return SimpleNamespace(
        session={"user_id": user_id}, client=SimpleNamespace(host="127.0.0.1")
    )


def _capture(monkeypatch):
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )


def test_parse_archive_month():
    assert archive_router_mod.parse_archive_month("2026-07") == (2026, 7)
    assert archive_router_mod.parse_archive_month("2026-12") == (2026, 12)
    assert archive_router_mod.parse_archive_month("2026-13") is None
    assert archive_router_mod.parse_archive_month("nope") is None
    assert archive_router_mod.parse_archive_month("") is None


def test_order_is_archived_predicate():
    # Робоча доба, не календарна: продакшн порівнює саме з нею, і між 00:00 і
    # 07:30 календарна дата дає інший день (T.3).
    today = business_today()
    cutoff = today - timedelta(days=RETENTION_DAYS)
    old_tab = (today - timedelta(days=90)).strftime("%d.%m.%y")

    aged = Order(source="lab", sheet_tab=old_tab)
    active = Order(source="lab", sheet_tab=today.strftime("%d.%m.%y"))
    archived_in_window = Order(
        source="lab", sheet_tab=today.strftime("%d.%m.%y"), archived_at=datetime.utcnow()
    )

    assert order_is_archived(aged, cutoff) is True
    assert order_is_archived(active, cutoff) is False
    assert order_is_archived(archived_in_window, cutoff) is True


def _seed(db):
    # Робоча доба, не календарна: продакшн порівнює саме з нею, і між 00:00 і
    # 07:30 календарна дата дає інший день (T.3).
    today = business_today()
    old = (today - timedelta(days=75))  # comfortably archived
    tab = old.strftime("%d.%m.%y")
    db.add_all([
        Order(source="lab", sheet_tab=tab, work_order_no="111"),
        Order(source="lab", sheet_tab=tab, work_order_no="222"),
        # active, in-window → must NOT appear in the archive
        Order(source="lab", sheet_tab=today.strftime("%d.%m.%y"), work_order_no="999"),
    ])
    db.commit()
    return old


def test_archive_holds_the_whole_history_not_only_what_fell_out(monkeypatch):
    """Архів показує ВСЮ історію, а не лише те, що випало з черги.

    Доти екран, названий «Архів» і збудований як календар по днях, показував
    за 3 вересня 10 робіт із 74 — решта ще жили в черзі. На питання «що було
    того дня» він відповідав уламком, і це читалось як втрата даних
    (зауваження власника 08.09.26).
    """
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = _seed(db)
        ctx = archive_router_mod.get_archive(request=_request(user.id), db=db)

    assert ctx["archive_total"] == 3, "жива робота теж належить історії"
    months = {m["ym"]: m["count"] for m in ctx["months"]}
    assert months.get(f"{old.year:04d}-{old.month:02d}") == 2
    today = business_today()
    assert months.get(f"{today.year:04d}-{today.month:02d}") == 1
    # Розкритий найсвіжіший місяць — там, де оператор працює.
    assert ctx["active_ym"] == f"{today.year:04d}-{today.month:02d}"


def test_archive_detail_partial_builds_calendar(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = _seed(db)
        ym = f"{old.year:04d}-{old.month:02d}"
        ctx = archive_router_mod.get_archive_detail(
            request=_request(user.id), month=ym, db=db
        )
    assert ctx["month_total"] == 2
    assert ctx["month_max"] == 2
    counts = [c["count"] for week in ctx["month_grid"] for c in week if c]
    assert 2 in counts


def test_archive_day_partial_lists_works_with_passport_link(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = _seed(db)
        ctx = archive_router_mod.get_archive_day(
            request=_request(user.id), date_param=old.strftime("%d.%m.%y"), db=db
        )
    assert [o.work_order_no for o in ctx["day_orders"]] == ["111", "222"]
    assert ctx["selected_date"] == old


def test_archive_search_matches_across_days(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _seed(db)
        ctx = archive_router_mod.get_archive_search(
            request=_request(user.id), q="111", db=db
        )
    assert ctx["result_total"] == 1
    assert ctx["results"][0]["order"].work_order_no == "111"
    assert ctx["results"][0]["date_label"]


def test_archive_search_empty_returns_latest_month(monkeypatch):
    """Порожній запит повертає найсвіжіший місяць — очищення поля не лишає діру."""
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        _seed(db)
        ctx = archive_router_mod.get_archive_search(
            request=_request(user.id), q="   ", db=db
        )
    # Найсвіжіший місяць — поточний: у ньому жива робота, і історія її містить.
    today = business_today()
    assert ctx["month_total"] == 1
    assert ctx["month_ym"] == f"{today.year:04d}-{today.month:02d}"


def test_order_detail_read_only_for_archived_editable_for_active(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    # Робоча доба, не календарна: продакшн порівнює саме з нею, і між 00:00 і
    # 07:30 календарна дата дає інший день (T.3).
    today = business_today()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = today - timedelta(days=75)
        archived = Order(source="lab", sheet_tab=old.strftime("%d.%m.%y"), work_order_no="A")
        active = Order(source="lab", sheet_tab=today.strftime("%d.%m.%y"), work_order_no="B")
        db.add_all([archived, active])
        db.commit()

        arch_ctx = orders_router_mod.get_order_detail(request=_request(user.id), order_id=archived.id, db=db)
        act_ctx = orders_router_mod.get_order_detail(request=_request(user.id), order_id=active.id, db=db)

    assert arch_ctx["read_only"] is True
    assert act_ctx["read_only"] is False


def test_a_day_shows_every_work_and_marks_only_the_vanished_ones(monkeypatch):
    """Головне, заради чого Архів переробили: день показується ЦІЛКОМ.

    Раніше сюди потрапляли лише роботи поза робочим вікном, і за 3 вересня
    оператор бачив 10 рядків із 74 — решта жили в черзі. Виглядало як втрата
    історії. Тепер день повний, а «зникла з таблиці» — окрема ознака рядка,
    а не умова потрапляння на екран.
    """
    _capture(monkeypatch)
    engine = _database()
    today = business_today()
    tab = today.strftime("%d.%m.%y")
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add_all([
            Order(source="lab", sheet_tab=tab, row_number=1, work_order_no="111"),
            Order(source="lab", sheet_tab=tab, row_number=2, work_order_no="222",
                  archived_at=datetime.utcnow()),
            Order(source="sheet_client", sheet_tab=tab, row_number=3, quantity="6",
                  material_color="pmma a2"),
        ])
        db.commit()
        gone_id = db.query(Order).filter(Order.work_order_no == "222").one().id

        ctx = archive_router_mod.get_archive_day(
            request=_request(user.id), date_param=tab, db=db
        )

    # Усі три роботи дня, у порядку таблиці — включно з живими.
    assert [o.row_number for o in ctx["day_orders"]] == [1, 2, 3]
    # Позначена лише та, чий рядок зник із таблиці.
    assert ctx["gone_ids"] == {gone_id}


def test_a_work_still_in_the_queue_is_not_marked_as_vanished(monkeypatch):
    """Ознака має означати ЗНИКНЕННЯ рядка, а не вік роботи: інакше через
    місяць увесь архів був би червоним, і мітка перестала б щось значити."""
    _capture(monkeypatch)
    engine = _database()
    aged = (business_today() - timedelta(days=75))
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(Order(source="lab", sheet_tab=aged.strftime("%d.%m.%y"),
                     row_number=1, work_order_no="777"))
        db.commit()

        ctx = archive_router_mod.get_archive_day(
            request=_request(user.id), date_param=aged.strftime("%d.%m.%y"), db=db
        )

    assert len(ctx["day_orders"]) == 1, "стара робота лишається в історії"
    assert ctx["gone_ids"] == set(), "вік — не зникнення"
