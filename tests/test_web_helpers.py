import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.models import EmailMessage, Order, User
from app.sheet_sync_service import SheetSyncSummary
from app.web import (
    _handout_pending_client_count,
    _order_date,
    _pluralize_uk,
    _queue_handout_summary,
    _queue_sort_key,
    _sync_summary_message,
    _write_sheet_fields,
    accept_email,
    get_current_user,
)


def test_email_order_uses_created_date():
    order = SimpleNamespace(sheet_tab=None, created_at=datetime(2026, 7, 28, 20, 10))
    assert _order_date(order) == date(2026, 7, 28)


def test_email_order_uses_kyiv_business_date_at_utc_boundary():
    order = SimpleNamespace(sheet_tab=None, created_at=datetime(2026, 7, 28, 22, 10))
    assert _order_date(order) == date(2026, 7, 29)


def test_sheet_tab_date_takes_precedence():
    order = SimpleNamespace(sheet_tab="01.08.26", created_at=datetime(2026, 7, 28, 23, 10))
    assert _order_date(order) == date(2026, 8, 1)


def test_inactive_user_session_is_revoked():
    request = SimpleNamespace(session={"user_id": 7})
    db = SimpleNamespace(get=lambda model, user_id: SimpleNamespace(is_active=False))

    assert get_current_user(request, db) is None
    assert request.session == {}


def test_active_user_session_remains_valid():
    user = SimpleNamespace(is_active=True)
    request = SimpleNamespace(session={"user_id": 7})
    db = SimpleNamespace(get=lambda model, user_id: user)

    assert get_current_user(request, db) is user
    assert request.session == {"user_id": 7}


def test_queue_sorts_earlier_deadline_first_for_same_day():
    base = {
        "sheet_tab": "01.08.26",
        "created_at": datetime(2026, 8, 1),
        "status": "нове",
    }
    later = SimpleNamespace(**base, due_time="16:00", id=1)
    earlier = SimpleNamespace(**base, due_time="09:00", id=2)

    assert sorted([later, earlier], key=_queue_sort_key) == [earlier, later]


def test_sync_summary_message_reports_import_counts():
    summary = SheetSyncSummary(tabs_processed=2, created=7, updated=3, unchanged=4)

    message = _sync_summary_message(summary)

    assert "вкладок: 2" in message
    assert "Нових робіт: 7" in message
    assert "оновлено: 3" in message


def test_accept_email_redirects_to_email_queue_filter():
    user = SimpleNamespace(id=7, username="operator", is_active=True)
    email = SimpleNamespace(id=9, status="нове", attachments_status="ready", attachments=[], order_id=None)

    class FakeDb:
        def __init__(self):
            self.added = []
            self.committed = False

        def get(self, model, object_id):
            if model is User:
                return user
            if model is EmailMessage:
                return email
            return None

        def add(self, value):
            self.added.append(value)

        def flush(self):
            order = next(value for value in self.added if isinstance(value, Order))
            order.id = 42

        def commit(self):
            self.committed = True

    db = FakeDb()
    request = SimpleNamespace(session={"user_id": user.id})

    # Without this, accept_email would fall through to the real
    # .env-based Google credentials (FakeDb isn't a real Session, so the
    # settings-store lookup returns None) and make a live network call —
    # this test only cares about the queue-redirect behavior, so the
    # sheet-note write-back is stubbed out at the boundary instead.
    with patch("app.web.open_spreadsheet"), patch(
        "app.web.get_worksheet_by_name", return_value=None
    ):
        response = asyncio.run(
            accept_email(
                request=request,
                email_id=email.id,
                client_name="Клієнт",
                material_color="моно A2",
                kind="анатомія",
                quantity="1",
                db=db,
            )
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?source=email"
    assert email.status == "прийнято"
    assert email.order_id == 42
    assert db.committed is True

    order = next(value for value in db.added if isinstance(value, Order))
    # Dated like a real наряд so period tabs / is_overdue() treat a priced
    # mail order the same as a table one — see accept_email's comment.
    assert order.sheet_tab == date.today().strftime("%d.%m.%y")
    assert order.row_number is None


def test_accept_email_refuses_while_attachments_still_downloading():
    """Two-phase mail fetch (app.mail_reader.fetch_new_emails): accepting an
    email whose attachments are still "pending" would create an order with
    zero attachments and orphan the files phase 2 saves afterward, since
    nothing later ever moves them into export. Must refuse, not proceed."""
    user = SimpleNamespace(id=7, username="operator", is_active=True)
    email = SimpleNamespace(id=9, status="нове", attachments_status="pending", attachments=[], order_id=None)

    class FakeDb:
        def __init__(self):
            self.added = []
            self.committed = False

        def get(self, model, object_id):
            if model is User:
                return user
            if model is EmailMessage:
                return email
            return None

        def add(self, value):
            self.added.append(value)

        def commit(self):
            self.committed = True

    db = FakeDb()
    request = SimpleNamespace(session={"user_id": user.id})

    response = asyncio.run(
        accept_email(
            request=request,
            email_id=email.id,
            client_name="Клієнт",
            material_color="моно A2",
            kind="анатомія",
            quantity="1",
            db=db,
        )
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/mail/9?error=")
    assert email.status == "нове"
    assert email.order_id is None
    assert db.committed is False
    assert not any(isinstance(value, Order) for value in db.added)


def test_write_sheet_fields_skips_email_orders_even_with_sheet_tab():
    """Email orders now carry a sheet_tab-shaped business date (see above),
    but they were never a real spreadsheet row — source, not sheet_tab, must
    gate the write-back, or this would try (and fail/misfire) against a
    worksheet row that doesn't correspond to this order."""
    order = SimpleNamespace(source="email", sheet_tab=date.today().strftime("%d.%m.%y"))
    db = SimpleNamespace(add=lambda value: (_ for _ in ()).throw(AssertionError("should not touch the sheet")))

    result = _write_sheet_fields(db, order, {"sum3d_id"})

    assert result is None


def test_pluralize_uk_picks_the_right_form():
    # Queue dashboard peek card grammar (CLAUDE.md screen 1 "Клієнти без
    # видачі"/"Ранкова видача" peek) — 1/21/31 клієнт, 2-4/22-24 клієнти,
    # 5-20/11-14/25 клієнтів.
    assert _pluralize_uk(1, "один", "два-чотири", "багато") == "один"
    assert _pluralize_uk(21, "один", "два-чотири", "багато") == "один"
    assert _pluralize_uk(2, "один", "два-чотири", "багато") == "два-чотири"
    assert _pluralize_uk(4, "один", "два-чотири", "багато") == "два-чотири"
    assert _pluralize_uk(5, "один", "два-чотири", "багато") == "багато"
    assert _pluralize_uk(11, "один", "два-чотири", "багато") == "багато"
    assert _pluralize_uk(12, "один", "два-чотири", "багато") == "багато"


def test_handout_pending_client_count_excludes_issued_and_future_orders():
    today = date(2026, 8, 9)
    yesterday_tab = "08.08.26"
    today_tab = "09.08.26"
    orders = [
        # Pending client from a past tab — counts.
        SimpleNamespace(client_name="Іванов", status="нове", sheet_tab=yesterday_tab),
        # Same client, second pending order — still one distinct client.
        SimpleNamespace(client_name="Іванов", status="прораховано", sheet_tab=yesterday_tab),
        # Already issued — excluded.
        SimpleNamespace(client_name="Петренко", status="видано", sheet_tab=yesterday_tab),
        # Future tab (today or later) — not yet due for handout, excluded.
        SimpleNamespace(client_name="Сидоренко", status="нове", sheet_tab=today_tab),
        # No client name (lab order without a client) — excluded.
        SimpleNamespace(client_name=None, status="нове", sheet_tab=yesterday_tab),
        # Email order with no sheet tab — always a handout candidate.
        SimpleNamespace(client_name="Коваль", status="нове", sheet_tab=None),
    ]

    assert _handout_pending_client_count(orders, today) == 2


def test_queue_handout_summary_reports_zero_as_all_issued():
    today = date(2026, 8, 9)
    orders = [SimpleNamespace(client_name="Іванов", status="видано", sheet_tab="08.08.26")]

    assert _queue_handout_summary(orders, today) == "Усе видано"


def test_queue_handout_summary_pluralizes_client_count():
    today = date(2026, 8, 9)
    orders = [
        SimpleNamespace(client_name="Іванов", status="нове", sheet_tab="08.08.26"),
        SimpleNamespace(client_name="Петренко", status="нове", sheet_tab="08.08.26"),
    ]

    assert _queue_handout_summary(orders, today) == "2 клієнти очікують"
