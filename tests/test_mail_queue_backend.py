import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Attachment, EmailMessage, Order, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Operator")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None, host: str = "127.0.0.1"):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, client=SimpleNamespace(host=host))


def test_queue_eagerly_exposes_pending_mail_oldest_first_independent_of_filters(
    tmp_path, monkeypatch
):
    engine = _database()
    mail_root = tmp_path / "mail"
    mail_root.mkdir()
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        now = datetime.now()
        newer = EmailMessage(uid="newer", status="нове", received_at=now)
        older = EmailMessage(uid="older", status="нове", received_at=now - timedelta(hours=1))
        accepted = EmailMessage(uid="accepted", status="прийнято", received_at=now - timedelta(days=1))
        db.add_all([newer, older, accepted])
        db.add(Order(source="lab", sheet_tab=date.today().strftime("%d.%m.%y")))
        db.commit()

        context = web.get_queue(
            request=_request(user.id), period="today", ready="all", source="lab", db=db
        )

        assert [email.uid for email in context["pending_emails"]] == ["older", "newer"]
        assert context["pending_mail_count"] == 2
        assert all(email.folder_available is False for email in context["pending_emails"])


def _call_get_queue(db, user, monkeypatch, tmp_path, **kwargs):
    """Same monkeypatch set as the pending-mail test above, factored out so
    the day-strip tests below don't need to repeat it: stubs the mail-export
    path lookup and captures the template context dict instead of rendering
    real HTML."""
    mail_root = tmp_path / "mail"
    mail_root.mkdir(exist_ok=True)
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    kwargs.setdefault("period", "today")
    kwargs.setdefault("ready", "all")
    kwargs.setdefault("source", "all")
    return web.get_queue(request=_request(user.id), db=db, **kwargs)


def test_known_order_dates_derived_from_distinct_sheet_tabs(tmp_path):
    """This is the crux of "stays synced with the Sheet": the day-strip's
    list of known days is read straight off Order.sheet_tab, which
    app/sync.py populates verbatim from real sheet tab names — no separate
    lookup against Google Sheets, no independently-computed guess."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        db.add_all(
            [
                Order(source="lab", sheet_tab="08.08.26", row_number=1),
                Order(source="lab", sheet_tab="08.08.26", row_number=2),  # duplicate tab
                Order(source="lab", sheet_tab="05.08.26", row_number=3),
                Order(source="email", sheet_tab="09.08.26"),
                Order(source="email", sheet_tab=None),  # unpriced mail order, no tab yet
            ]
        )
        db.commit()

        known = web._known_order_dates(db)

        assert known == [date(2026, 8, 5), date(2026, 8, 8), date(2026, 8, 9)]


def test_date_filter_returns_exactly_that_days_orders(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        matching = Order(source="lab", sheet_tab="08.08.26", client_name="Іванов")
        other_day = Order(source="lab", sheet_tab="09.08.26", client_name="Петренко")
        db.add_all([matching, other_day])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, date_param="08.08.26")

        assert [o.id for o in context["orders"]] == [matching.id]
        assert context["selected_date"] == date(2026, 8, 8)


def test_date_filter_bypasses_period_bucket_entirely(tmp_path, monkeypatch):
    """A `date` far outside today/yesterday/tomorrow must still surface its
    orders even though `period` defaults to "today" — the same bypass
    `show_overdue` already gets over the period bucket."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        far_past = Order(source="lab", sheet_tab="01.01.26", client_name="Далеко")
        db.add(far_past)
        db.commit()

        context = _call_get_queue(
            db, user, monkeypatch, tmp_path, period="today", date_param="01.01.26"
        )

        assert [o.id for o in context["orders"]] == [far_past.id]


def test_date_filter_composes_with_source_and_ready_like_period_does(tmp_path, monkeypatch):
    """`date` is an independent, additive filter — `source`/`ready` must
    still narrow the result the same way they narrow a plain period bucket."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        lab_order = Order(source="lab", sheet_tab="08.08.26", client_name="Іванов")
        email_order = Order(source="email", sheet_tab="08.08.26", client_name="Петренко")
        db.add_all([lab_order, email_order])
        db.commit()

        all_sources = _call_get_queue(db, user, monkeypatch, tmp_path, date_param="08.08.26")
        assert {o.id for o in all_sources["orders"]} == {lab_order.id, email_order.id}

        lab_only = _call_get_queue(
            db, user, monkeypatch, tmp_path, date_param="08.08.26", source="lab"
        )
        assert [o.id for o in lab_only["orders"]] == [lab_order.id]

        email_only = _call_get_queue(
            db, user, monkeypatch, tmp_path, date_param="08.08.26", source="email"
        )
        assert [o.id for o in email_only["orders"]] == [email_order.id]


def test_invalid_date_param_falls_back_to_period_bucketing(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_order = Order(source="lab", sheet_tab=date.today().strftime("%d.%m.%y"))
        db.add(today_order)
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, date_param="not-a-date")

        assert context["selected_date"] is None
        assert [o.id for o in context["orders"]] == [today_order.id]


def test_omitting_sort_preserves_default_urgency_ordering(tmp_path, monkeypatch):
    """Regression guard: the new opt-in `sort` param must not change the
    default queue ordering when absent — same earliest-deadline-first
    urgency ordering _queue_sort_key already guarantees (see
    test_web_helpers.test_queue_sorts_earlier_deadline_first_for_same_day)."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        later = Order(source="lab", sheet_tab=today_tab, due_time="16:00", material_color="я останній")
        earlier = Order(source="lab", sheet_tab=today_tab, due_time="09:00", material_color="а перший")
        db.add_all([later, earlier])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path)

        assert [o.id for o in context["orders"]] == [earlier.id, later.id]
        assert context["sort"] == ""
        assert context["sort_dir"] == "asc"


def test_sort_by_material_ascending_is_case_insensitive(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        titan = Order(source="lab", sheet_tab=today_tab, material_color="Титан")
        mono = Order(source="lab", sheet_tab=today_tab, material_color="моно")
        pmma = Order(source="lab", sheet_tab=today_tab, material_color="ПММА")
        db.add_all([titan, mono, pmma])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, sort="material", sort_dir="asc")

        assert [o.id for o in context["orders"]] == [mono.id, pmma.id, titan.id]
        assert context["sort"] == "material"


def test_sort_by_kind_case_insensitive(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        vklad = Order(source="lab", sheet_tab=today_tab, kind="вкладка")
        anatomia = Order(source="lab", sheet_tab=today_tab, kind="Анатомія")
        db.add_all([vklad, anatomia])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, sort="kind", sort_dir="asc")

        assert [o.id for o in context["orders"]] == [anatomia.id, vklad.id]


def test_sort_by_material_blank_values_sort_last_both_directions(tmp_path, monkeypatch):
    """Blanks must stay last regardless of asc/desc — an operator sorting
    "by material" descending still doesn't want blanks floating to the top."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        blank = Order(source="lab", sheet_tab=today_tab, material_color=None)
        pmma = Order(source="lab", sheet_tab=today_tab, material_color="пмма")
        db.add_all([blank, pmma])
        db.commit()

        asc = _call_get_queue(db, user, monkeypatch, tmp_path, sort="material", sort_dir="asc")
        desc = _call_get_queue(db, user, monkeypatch, tmp_path, sort="material", sort_dir="desc")

        assert [o.id for o in asc["orders"]] == [pmma.id, blank.id]
        assert [o.id for o in desc["orders"]] == [pmma.id, blank.id]


def test_sort_by_quantity_is_numeric_not_lexicographic(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        ten = Order(source="lab", sheet_tab=today_tab, quantity="10")
        three = Order(source="lab", sheet_tab=today_tab, quantity="3")
        db.add_all([ten, three])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, sort="quantity", sort_dir="asc")

        # Numeric, not lexicographic: 3 < 10 (a string sort would put "10" first).
        assert [o.id for o in context["orders"]] == [three.id, ten.id]


def test_sort_by_quantity_non_numeric_sorts_last(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        numeric = Order(source="lab", sheet_tab=today_tab, quantity="5")
        junk = Order(source="lab", sheet_tab=today_tab, quantity="кілька")
        db.add_all([junk, numeric])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, sort="quantity", sort_dir="asc")

        assert [o.id for o in context["orders"]] == [numeric.id, junk.id]


def test_sort_direction_toggles_order(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        low = Order(source="lab", sheet_tab=today_tab, quantity="1")
        high = Order(source="lab", sheet_tab=today_tab, quantity="9")
        db.add_all([low, high])
        db.commit()

        asc = _call_get_queue(db, user, monkeypatch, tmp_path, sort="quantity", sort_dir="asc")
        desc = _call_get_queue(db, user, monkeypatch, tmp_path, sort="quantity", sort_dir="desc")

        assert [o.id for o in asc["orders"]] == [low.id, high.id]
        assert [o.id for o in desc["orders"]] == [high.id, low.id]
        assert desc["sort_dir"] == "desc"


def test_invalid_sort_field_is_ignored(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        db.add(Order(source="lab", sheet_tab=today_tab, due_time="09:00"))
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, sort="not-a-real-field")

        assert context["sort"] == ""


def test_open_mail_folder_requires_authentication(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine) as db, pytest.raises(HTTPException) as exc:
        web.open_mail_folder(request=_request(None), email_id=1, db=db)
    assert exc.value.status_code == 401


def test_open_mail_folder_rejects_non_loopback(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        with pytest.raises(HTTPException) as exc:
            web.open_mail_folder(
                request=_request(user.id, "192.168.1.20"), email_id=1, db=db
            )
    assert exc.value.status_code == 403


def test_open_mail_folder_opens_safe_db_path_and_returns_no_content(tmp_path, monkeypatch):
    engine = _database()
    mail_root = tmp_path / "mail"
    folder = mail_root / "message-1"
    folder.mkdir(parents=True)
    file = folder / "case.stl"
    file.write_bytes(b"mesh")
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    opened: list = []
    monkeypatch.setattr(web, "_open_folder_in_explorer", opened.append)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="mail-1", status="нове")
        db.add(email)
        db.flush()
        db.add(
            Attachment(
                email_message_id=email.id,
                filename="case.stl",
                saved_path=str(file),
            )
        )
        db.commit()

        response = web.open_mail_folder(
            request=_request(user.id), email_id=email.id, db=db
        )

    assert response.status_code == 204
    assert opened == [folder.resolve()]


def test_open_mail_folder_rejects_db_path_outside_roots(tmp_path, monkeypatch):
    engine = _database()
    mail_root = tmp_path / "mail"
    mail_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    file = outside / "case.stl"
    file.write_bytes(b"mesh")
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="mail-1", status="нове")
        db.add(email)
        db.flush()
        db.add(Attachment(email_message_id=email.id, filename="case.stl", saved_path=str(file)))
        db.commit()

        with pytest.raises(HTTPException) as exc:
            web.open_mail_folder(request=_request(user.id), email_id=email.id, db=db)

    assert exc.value.status_code == 404


def test_queue_splits_orders_into_lab_and_email_groups(tmp_path, monkeypatch):
    """Item 4: queue.html renders two independently collapsible sections
    ("Лабораторні роботи" / "Роботи з пошти") from server-split lists rather
    than filtering in the template, so per-column sort/filtering keeps
    working correctly within each group."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        lab_order = Order(source="lab", sheet_tab=today_tab, client_name="Іванов")
        email_order = Order(source="email", sheet_tab=today_tab, client_name="Петренко")
        db.add_all([lab_order, email_order])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path)

        assert [o.id for o in context["orders_lab"]] == [lab_order.id]
        assert [o.id for o in context["orders_email"]] == [email_order.id]
        # The combined `orders` list (used for the overall page summary/
        # empty-state check) must still contain both.
        assert {o.id for o in context["orders"]} == {lab_order.id, email_order.id}


def test_queue_split_preserves_column_sort_within_each_group(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        lab_b = Order(source="lab", sheet_tab=today_tab, material_color="я останній")
        lab_a = Order(source="lab", sheet_tab=today_tab, material_color="а перший")
        email_b = Order(source="email", sheet_tab=today_tab, material_color="я останній")
        email_a = Order(source="email", sheet_tab=today_tab, material_color="а перший")
        db.add_all([lab_b, lab_a, email_b, email_a])
        db.commit()

        context = _call_get_queue(db, user, monkeypatch, tmp_path, sort="material", sort_dir="asc")

        assert [o.id for o in context["orders_lab"]] == [lab_a.id, lab_b.id]
        assert [o.id for o in context["orders_email"]] == [email_a.id, email_b.id]


def test_queue_split_respects_existing_source_filter(tmp_path, monkeypatch):
    """When the sidebar's own "Джерело" filter narrows to one source, the
    other group's list must come back empty (queue.html skips rendering an
    empty section entirely) rather than showing a stale/duplicated group."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        today_tab = date.today().strftime("%d.%m.%y")
        lab_order = Order(source="lab", sheet_tab=today_tab)
        email_order = Order(source="email", sheet_tab=today_tab)
        db.add_all([lab_order, email_order])
        db.commit()

        lab_only = _call_get_queue(db, user, monkeypatch, tmp_path, source="lab")
        assert [o.id for o in lab_only["orders_lab"]] == [lab_order.id]
        assert lab_only["orders_email"] == []

        email_only = _call_get_queue(db, user, monkeypatch, tmp_path, source="email")
        assert email_only["orders_lab"] == []
        assert [o.id for o in email_only["orders_email"]] == [email_order.id]


def test_reject_email_marks_rejected_and_excludes_from_triage_list(tmp_path, monkeypatch):
    """Item 7's triage-list reject button posts to this same, already-
    existing route (app/web.py::reject_email) — confirm end-to-end that
    rejecting removes the email from get_mail's "нове" query without
    touching the real IMAP mailbox (no imap_tools call happens here at
    all — mark_seen semantics live entirely in app/mail_reader.py)."""
    engine = _database()
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="reject-me", status="нове", from_address="client@example.test")
        db.add(email)
        db.commit()

        response = asyncio.run(
            web.reject_email(request=_request(user.id), email_id=email.id, db=db)
        )
        assert response.status_code == 303

        db.refresh(email)
        assert email.status == "відхилено"

        mail_context = web.get_mail(request=_request(user.id), db=db)
        assert email.id not in [e.id for e in mail_context["emails"]]


def test_open_mail_folder_reports_non_windows_backend(tmp_path, monkeypatch):
    engine = _database()
    mail_root = tmp_path / "mail"
    mail_root.mkdir()
    file = mail_root / "case.stl"
    file.write_bytes(b"mesh")
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    monkeypatch.setattr(
        web, "_open_folder_in_explorer", lambda _folder: (_ for _ in ()).throw(NotImplementedError())
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="mail-1", status="нове")
        db.add(email)
        db.flush()
        db.add(Attachment(email_message_id=email.id, filename="case.stl", saved_path=str(file)))
        db.commit()

        with pytest.raises(HTTPException) as exc:
            web.open_mail_folder(request=_request(user.id), email_id=email.id, db=db)

    assert exc.value.status_code == 501
