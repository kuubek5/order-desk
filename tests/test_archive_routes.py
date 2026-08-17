from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
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
    assert web._parse_archive_month("2026-07") == (2026, 7)
    assert web._parse_archive_month("2026-12") == (2026, 12)
    assert web._parse_archive_month("2026-13") is None
    assert web._parse_archive_month("nope") is None
    assert web._parse_archive_month("") is None


def test_order_is_archived_predicate():
    today = date.today()
    cutoff = today - timedelta(days=web.RETENTION_DAYS)
    old_tab = (today - timedelta(days=90)).strftime("%d.%m.%y")

    aged = Order(source="lab", sheet_tab=old_tab)
    active = Order(source="lab", sheet_tab=today.strftime("%d.%m.%y"))
    archived_in_window = Order(
        source="lab", sheet_tab=today.strftime("%d.%m.%y"), archived_at=datetime.utcnow()
    )

    reactivated_old = Order(
        source="lab", sheet_tab=old_tab, reactivated_at=datetime.utcnow()
    )

    assert web._order_is_archived(aged, cutoff) is True
    assert web._order_is_archived(active, cutoff) is False
    assert web._order_is_archived(archived_in_window, cutoff) is True
    # Pulled back out of the archive → active again despite the old date.
    assert web._order_is_archived(reactivated_old, cutoff) is False


def _seed(db):
    today = date.today()
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


def test_archive_months_level_excludes_active(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = _seed(db)
        ctx = web.get_archive(request=_request(user.id), db=db)
    assert ctx["level"] == "months"
    assert ctx["archive_total"] == 2  # the active order is excluded
    ym = f"{old.year:04d}-{old.month:02d}"
    months = {m["ym"]: m["count"] for m in ctx["months"]}
    assert months.get(ym) == 2


def test_archive_month_level_builds_calendar(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = _seed(db)
        ym = f"{old.year:04d}-{old.month:02d}"
        ctx = web.get_archive(request=_request(user.id), month=ym, db=db)
    assert ctx["level"] == "month"
    assert ctx["month_total"] == 2
    assert ctx["month_max"] == 2
    # The seeded day cell carries the right count somewhere in the grid.
    counts = [c["count"] for week in ctx["month_grid"] for c in week if c]
    assert 2 in counts


def test_archive_day_level_lists_works_with_passport_link(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = _seed(db)
        ctx = web.get_archive(
            request=_request(user.id), date_param=old.strftime("%d.%m.%y"), db=db
        )
    assert ctx["level"] == "day"
    assert [o.work_order_no for o in ctx["day_orders"]] == ["111", "222"]
    assert ctx["selected_date"] == old


def test_order_detail_read_only_for_archived_editable_for_active(monkeypatch):
    _capture(monkeypatch)
    engine = _database()
    today = date.today()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = today - timedelta(days=75)
        archived = Order(source="lab", sheet_tab=old.strftime("%d.%m.%y"), work_order_no="A")
        active = Order(source="lab", sheet_tab=today.strftime("%d.%m.%y"), work_order_no="B")
        db.add_all([archived, active])
        db.commit()

        arch_ctx = web.get_order_detail(request=_request(user.id), order_id=archived.id, db=db)
        act_ctx = web.get_order_detail(request=_request(user.id), order_id=active.id, db=db)

    assert arch_ctx["read_only"] is True
    assert act_ctx["read_only"] is False


def test_unarchive_order_returns_it_to_the_queue():
    from app.models import StatusEvent

    engine = _database()
    today = date.today()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        old = today - timedelta(days=75)
        order = Order(
            source="lab", sheet_tab=old.strftime("%d.%m.%y"), work_order_no="Z",
            status="відфрезеровано", archived_at=datetime.utcnow(),
        )
        db.add(order)
        db.commit()
        oid = order.id

        resp = web.unarchive_order(request=_request(user.id), order_id=oid, db=db)

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/orders/{oid}"
        db.refresh(order)
        assert order.archived_at is None
        assert order.reactivated_at is not None
        # No longer archived → would show in the working queue again.
        cutoff = today - timedelta(days=web.RETENTION_DAYS)
        assert web._order_is_archived(order, cutoff) is False
        # Audit trace records who did it, without changing the real status.
        ev = db.scalar(select(StatusEvent).where(StatusEvent.status == "розархівовано"))
        assert ev is not None and ev.operator_id == user.id
        assert order.status == "відфрезеровано"
