import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.auth import hash_password
from app.models import EmailMessage, Order, User
from app.sheet_sync_service import SheetSyncSummary
from app.web import (
    MAIL_SYNC_INTERVAL_SECONDS,
    SHEET_SYNC_INTERVAL_SECONDS,
    _date_window,
    _handout_pending_client_count,
    _order_date,
    _pluralize_uk,
    _queue_column_sort_value,
    _queue_handout_summary,
    _queue_sort_key,
    _sort_orders_by_column,
    _sync_summary_message,
    _write_sheet_fields,
    accept_email,
    get_current_user,
    post_account_password,
)


def test_sheet_sync_interval_is_one_minute():
    """Confirmed safe to halve from the old 2-minute value: one sync cycle
    is ~4 Sheets API calls (worksheets() + get_all_values() per relevant
    tab), Google's quota is hundreds of reads/minute. Mail sync keeps its
    own, independent 2-minute interval."""
    assert SHEET_SYNC_INTERVAL_SECONDS == 60
    assert MAIL_SYNC_INTERVAL_SECONDS == 2 * 60


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


def test_column_sort_value_quantity_parses_numeric_string():
    order = SimpleNamespace(quantity="12")
    assert _queue_column_sort_value(order, "quantity") == 12


def test_column_sort_value_quantity_non_numeric_is_blank():
    order = SimpleNamespace(quantity="кілька")
    assert _queue_column_sort_value(order, "quantity") is None


def test_column_sort_value_material_lowercases_and_strips():
    order = SimpleNamespace(material_color="  ПММА A2 ")
    assert _queue_column_sort_value(order, "material") == "пмма a2"


def test_column_sort_value_material_blank_is_none():
    order = SimpleNamespace(material_color=None)
    assert _queue_column_sort_value(order, "material") is None
    order_empty = SimpleNamespace(material_color="   ")
    assert _queue_column_sort_value(order_empty, "material") is None


def test_column_sort_value_kind_reads_kind_field():
    order = SimpleNamespace(kind="Абатмент")
    assert _queue_column_sort_value(order, "kind") == "абатмент"


def test_sort_orders_by_column_material_ascending_case_insensitive():
    a = SimpleNamespace(material_color="титан", id=1)
    b = SimpleNamespace(material_color="Пмма", id=2)
    c = SimpleNamespace(material_color="моно", id=3)

    result = _sort_orders_by_column([a, b, c], "material", "asc")

    assert [o.id for o in result] == [3, 2, 1]  # моно, Пмма, титан


def test_sort_orders_by_column_material_blanks_sort_last_ascending():
    with_value = SimpleNamespace(material_color="пмма", id=1)
    blank = SimpleNamespace(material_color=None, id=2)

    result = _sort_orders_by_column([blank, with_value], "material", "asc")

    assert [o.id for o in result] == [1, 2]


def test_sort_orders_by_column_material_blanks_sort_last_descending():
    """Blanks must stay last even in descending order — an operator sorting
    "by material" descending still doesn't want blanks floating to the top."""
    with_value = SimpleNamespace(material_color="пмма", id=1)
    blank = SimpleNamespace(material_color="", id=2)

    result = _sort_orders_by_column([with_value, blank], "material", "desc")

    assert [o.id for o in result] == [1, 2]


def test_sort_orders_by_column_quantity_numeric_ascending():
    small = SimpleNamespace(quantity="3", id=1)
    big = SimpleNamespace(quantity="10", id=2)

    result = _sort_orders_by_column([big, small], "quantity", "asc")

    # Numeric, not lexicographic: 3 < 10 (a string sort would put "10" first).
    assert [o.id for o in result] == [1, 2]


def test_sort_orders_by_column_quantity_non_numeric_sorts_last():
    numeric = SimpleNamespace(quantity="5", id=1)
    junk = SimpleNamespace(quantity="кілька", id=2)
    blank = SimpleNamespace(quantity=None, id=3)

    result = _sort_orders_by_column([junk, blank, numeric], "quantity", "asc")

    assert result[0].id == 1
    assert {o.id for o in result[1:]} == {2, 3}


def test_sort_orders_by_column_direction_toggle():
    a = SimpleNamespace(quantity="1", id=1)
    b = SimpleNamespace(quantity="2", id=2)

    ascending = _sort_orders_by_column([a, b], "quantity", "asc")
    descending = _sort_orders_by_column([a, b], "quantity", "desc")

    assert [o.id for o in ascending] == [1, 2]
    assert [o.id for o in descending] == [2, 1]


def _dates(*day_month_years):
    """Small helper to build ascending lists of dates from '10.08.26'-style
    strings, matching the "%d.%m.%y" shape the day-strip works with."""
    return [datetime.strptime(s, "%d.%m.%y").date() for s in day_month_years]


def test_date_window_empty_known_dates_returns_nothing():
    assert _date_window([], date(2026, 8, 10), None) == ([], 0, 0)


def test_date_window_defaults_to_window_containing_today():
    known = _dates(
        "01.08.26", "02.08.26", "03.08.26", "04.08.26", "05.08.26", "06.08.26", "07.08.26",
        "08.08.26", "09.08.26", "10.08.26",
    )
    today = datetime.strptime("09.08.26", "%d.%m.%y").date()

    visible, page, total_pages = _date_window(known, today, None)

    # Pages tile from the newest end: page 0 is the newest full window ending at
    # the last date (known[3:10]), and it contains today.
    assert page == 0
    assert total_pages == 2
    assert visible == known[3:]
    assert today in visible


def test_date_window_defaults_to_most_recent_window_when_today_missing():
    known = _dates("01.08.26", "02.08.26", "03.08.26")
    today = datetime.strptime("20.08.26", "%d.%m.%y").date()

    visible, page, total_pages = _date_window(known, today, None)

    assert page == 0
    assert total_pages == 1
    assert visible == known


def test_date_window_explicit_page_is_used_verbatim():
    known = _dates(
        "01.08.26", "02.08.26", "03.08.26", "04.08.26", "05.08.26", "06.08.26", "07.08.26",
        "08.08.26", "09.08.26",
    )
    today = datetime.strptime("09.08.26", "%d.%m.%y").date()

    visible, page, total_pages = _date_window(known, today, 0)

    # Page 0 is the NEWEST window (right-tiled), i.e. the last 7 dates.
    assert page == 0
    assert total_pages == 2
    assert visible == known[2:]


def test_date_window_higher_page_steps_back_in_time():
    known = _dates(
        "01.08.26", "02.08.26", "03.08.26", "04.08.26", "05.08.26", "06.08.26", "07.08.26",
        "08.08.26", "09.08.26",
    )
    today = datetime.strptime("09.08.26", "%d.%m.%y").date()

    # Page 1 is the older window; with 9 dates and window 7 the oldest page is
    # the partial leading remainder.
    visible, page, total_pages = _date_window(known, today, 1)

    assert page == 1
    assert total_pages == 2
    assert visible == known[:2]


def test_date_window_clamps_page_beyond_available_range():
    known = _dates("01.08.26", "02.08.26", "03.08.26")
    today = date(2026, 8, 1)

    visible, page, total_pages = _date_window(known, today, 99)

    assert page == 0
    assert total_pages == 1
    assert visible == known


def test_date_window_clamps_negative_page_to_zero():
    known = _dates("01.08.26", "02.08.26", "03.08.26")
    today = date(2026, 8, 1)

    visible, page, total_pages = _date_window(known, today, -3)

    assert page == 0
    assert visible == known


def test_date_window_fewer_than_seven_known_dates():
    known = _dates("05.08.26", "06.08.26")
    today = date(2026, 8, 5)

    visible, page, total_pages = _date_window(known, today, None)

    assert total_pages == 1
    assert page == 0
    assert visible == known


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


def _fake_account_db(user):
    class FakeDb:
        def __init__(self):
            self.committed = False

        def get(self, model, object_id):
            return user if model is User else None

        def commit(self):
            self.committed = True

    return FakeDb()


def test_account_password_change_succeeds_with_correct_current_password():
    user = SimpleNamespace(id=1, is_active=True, password_hash=hash_password("old-pw"))
    db = _fake_account_db(user)
    request = SimpleNamespace(session={"user_id": user.id})

    response = asyncio.run(
        post_account_password(
            request=request,
            current_password="old-pw",
            new_password="new-secret",
            confirm_password="new-secret",
            db=db,
        )
    )

    assert response.template.name == "account.html"
    assert response.context["saved"] is True
    assert db.committed is True
    # The stored hash must actually change, not just report success.
    assert user.password_hash != hash_password("old-pw")


def test_account_password_change_rejects_wrong_current_password():
    original_hash = hash_password("old-pw")
    user = SimpleNamespace(id=1, is_active=True, password_hash=original_hash)
    db = _fake_account_db(user)
    request = SimpleNamespace(session={"user_id": user.id})

    response = asyncio.run(
        post_account_password(
            request=request,
            current_password="wrong-pw",
            new_password="new-secret",
            confirm_password="new-secret",
            db=db,
        )
    )

    assert response.context["error"] == "Поточний пароль невірний"
    assert user.password_hash == original_hash
    assert db.committed is False


def test_account_password_change_rejects_mismatched_confirmation():
    user = SimpleNamespace(id=1, is_active=True, password_hash=hash_password("old-pw"))
    db = _fake_account_db(user)
    request = SimpleNamespace(session={"user_id": user.id})

    response = asyncio.run(
        post_account_password(
            request=request,
            current_password="old-pw",
            new_password="new-secret",
            confirm_password="different",
            db=db,
        )
    )

    assert response.context["error"] == "Паролі не збігаються"
    assert db.committed is False
