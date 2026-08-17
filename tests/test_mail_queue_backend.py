import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Attachment, EmailMessage, Order, User
from app.parser import HEADER_ROWS


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


def test_partial_rows_renders_fragment_not_full_page(tmp_path, monkeypatch):
    """partial=rows must render the polled rows fragment (_queue_rows.html),
    while the default renders the whole queue page — the 15s auto-refresh
    depends on the route returning just the rows block."""
    engine = _database()
    mail_root = tmp_path / "mail"
    mail_root.mkdir()
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(mail_root))
    monkeypatch.setattr(web, "get_export_folder_path", lambda _db: "")
    captured = {}
    monkeypatch.setattr(
        web.templates,
        "TemplateResponse",
        lambda request, template, context: captured.update(template=template, ctx=context) or context,
    )

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(Order(source="lab", sheet_tab=date.today().strftime("%d.%m.%y")))
        db.commit()

        web.get_queue(request=_request(user.id), db=db, partial="rows")
        assert captured["template"] == "_queue_rows.html"
        # rows_qs carries the active filters so the poll re-requests this view.
        assert "period=today" in captured["ctx"]["rows_qs"]

        web.get_queue(request=_request(user.id), db=db)
        assert captured["template"] == "queue.html"


def test_sync_speed_route_switches_preset_and_rejects_unknown(tmp_path, monkeypatch):
    engine = _database()
    captured = {}
    monkeypatch.setattr(
        web.templates,
        "TemplateResponse",
        lambda request, template, context: captured.update(template=template, ctx=context) or context,
    )
    original = web._sync_speed_preset
    try:
        with Session(engine, expire_on_commit=False) as db:
            user = _user(db)

            web.set_sync_speed(request=_request(user.id), preset="turbo", db=db)
            assert web._sync_speed_preset == "turbo"
            assert web.get_sync_speed()["hot"] == 5
            assert captured["template"] == "_sync_speed_seg.html"
            assert captured["ctx"]["sync_speed_active"] == "turbo"

            # Unknown value degrades to a no-op, same as the queue filters.
            web.set_sync_speed(request=_request(user.id), preset="ludicrous", db=db)
            assert web._sync_speed_preset == "turbo"

            with pytest.raises(HTTPException):
                web.set_sync_speed(request=_request(None), preset="eco", db=db)
            assert web._sync_speed_preset == "turbo"
    finally:
        web._sync_speed_preset = original


def test_queue_records_viewed_day_for_hot_lane(tmp_path, monkeypatch):
    engine = _database()
    web._viewed_days.clear()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        db.add(Order(source="lab", sheet_tab="08.08.26", row_number=1, status="нове"))
        db.commit()

        _call_get_queue(db, user, monkeypatch, tmp_path, date_param="08.08.26")

    assert date(2026, 8, 8) in web._hot_extra_days()
    web._viewed_days.clear()
    assert web._hot_extra_days() == set()


def _make_tech_token(tech_root, job_code, monkeypatch):
    import app.stl_preview as stl_preview

    monkeypatch.setattr(stl_preview, "get_technician_files_path", lambda _db: str(tech_root))
    folder = tech_root / job_code
    folder.mkdir(parents=True)
    (folder / "crown.stl").write_bytes(b"solid mesh")
    token = stl_preview.build_preview_token(folder, {"tech": str(tech_root)})
    return token, folder


def test_open_preview_folder_opens_token_path(tmp_path, monkeypatch):
    engine = _database()
    tech = tmp_path / "tech"
    token, folder = _make_tech_token(tech, "2026-07-21_21112-001", monkeypatch)
    opened: list = []
    monkeypatch.setattr(web, "_open_folder_in_explorer", opened.append)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        response = web.open_preview_folder(request=_request(user.id), token=token, db=db)

    assert response.status_code == 204
    assert opened == [folder.resolve()]


def test_open_preview_folder_requires_authentication(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        with pytest.raises(HTTPException) as exc:
            web.open_preview_folder(request=_request(None), token="x", db=db)
    assert exc.value.status_code == 401


def test_open_preview_folder_rejects_non_loopback(tmp_path, monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        with pytest.raises(HTTPException) as exc:
            web.open_preview_folder(
                request=_request(user.id, host="10.0.0.9"), token="x", db=db
            )
    assert exc.value.status_code == 403


def test_open_preview_folder_bad_token_is_404(tmp_path, monkeypatch):
    engine = _database()
    monkeypatch.setattr(web, "_open_folder_in_explorer", lambda f: None)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        with pytest.raises(HTTPException) as exc:
            web.open_preview_folder(request=_request(user.id), token="not-a-real-token", db=db)
    assert exc.value.status_code == 404


def _stub_sheet_write(monkeypatch, note_rows=None, tab=None):
    """Patch the batch sheet writer. Captures the works list / placement /
    paint_blue and returns sheet rows (defaults to a contiguous block from 60)."""
    tab = tab or date.today().strftime("%d.%m.%y")
    fake_ws = SimpleNamespace(title=tab)
    cap = {}
    # Fresh double-submit dedup state per test so module-level state can't leak
    # between tests that share a user id + payload.
    monkeypatch.setattr(web, "_recent_manual_adds", {})
    monkeypatch.setattr(web, "open_spreadsheet", lambda db=None: object())
    monkeypatch.setattr(web, "get_worksheet_by_name", lambda ss, name: fake_ws)
    # The warm append now resolves the newest dated tab ≤ today itself.
    monkeypatch.setattr(web, "latest_worksheet_on_or_before", lambda ss, d: fake_ws)

    cap["calls"] = 0

    def _fake_append(ws, works, *, paint_blue, placement):
        cap["calls"] += 1
        cap["works"] = works
        cap["paint_blue"] = paint_blue
        cap["placement"] = placement
        return note_rows if note_rows is not None else [60 + i for i in range(len(works))]

    monkeypatch.setattr(web, "append_manual_work_rows", _fake_append)
    return cap


def _empty_form():
    """The list defaults for the manual-order endpoint's per-field parallel
    lists — merge overrides in per test."""
    return dict(
        client_name=[], work_order_no=[], kind=[], material_color=[],
        quantity=[], sum3d_id=[], job_code=[], technician_name=[],
    )


def test_create_manual_order_writes_client_row_and_creates_order(monkeypatch):
    engine = _database()
    cap = _stub_sheet_write(monkeypatch, note_rows=[65])
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="client", db=db,
            **{**_empty_form(), "client_name": ["Басараб"],
               "material_color": ["mono a3"], "quantity": ["2"]},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?source=client"
        assert cap["placement"] == "client"

        order = db.scalar(select(Order).where(Order.source == "sheet_client"))
        assert order.client_name == "Басараб"
        assert order.material_color == "mono a3"
        assert order.quantity == "2"
        assert order.sheet_tab == date.today().strftime("%d.%m.%y")
        assert order.row_number == 65 - HEADER_ROWS  # linked to the written row


def test_create_manual_lab_order_with_sum3d(monkeypatch):
    engine = _database()
    cap = _stub_sheet_write(monkeypatch, note_rows=[70])
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="lab", db=db,
            **{**_empty_form(), "work_order_no": ["24999"], "kind": ["анатомія"],
               "material_color": ["mono a2"], "quantity": ["3"], "sum3d_id": ["10-19-48"]},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?source=lab"

        order = db.scalar(select(Order).where(Order.source == "lab"))
        assert order.work_order_no == "24999"
        assert order.kind == "анатомія"
        assert order.sum3d_id == "10-19-48"
        assert order.client_name is None
        assert order.status == "прийнято"  # has a Sum3D
        assert order.row_number == 70 - HEADER_ROWS
        # The sheet append got наряд/вид/Sum3D and no blue.
        w = cap["works"][0]
        assert w["work_order_no"] == "24999"
        assert w["e_value"] == "анатомія"
        assert w["sum3d_id"] == "10-19-48"
        assert cap["paint_blue"] is False
        assert cap["placement"] == "lab"


def test_create_manual_lab_order_allows_missing_naryad(monkeypatch):
    """Lab works may be incomplete — no наряд, just a material — and still be
    written (наряд is often filled in later)."""
    engine = _database()
    cap = _stub_sheet_write(monkeypatch, note_rows=[32])
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="lab", db=db,
            **{**_empty_form(), "work_order_no": ["  "], "material_color": ["mono a2"]},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?source=lab"
        order = db.scalar(select(Order).where(Order.source == "lab"))
        assert order is not None
        assert order.work_order_no is None  # blank наряд stored as NULL
        assert order.material_color == "mono a2"
        assert cap["placement"] == "lab"


def test_create_manual_lab_order_rejects_fully_blank(monkeypatch):
    engine = _database()
    _stub_sheet_write(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="lab", db=db,
            **{**_empty_form(), "material_color": [""], "quantity": [""]},
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert db.scalar(select(Order)) is None


def test_create_manual_order_writes_job_code_and_technician(monkeypatch):
    engine = _database()
    cap = _stub_sheet_write(monkeypatch, note_rows=[33])
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="lab", db=db,
            **{**_empty_form(), "work_order_no": ["24500"], "kind": ["анатомія"],
               "material_color": ["цирконій"], "quantity": ["1"],
               "job_code": ["2026-07-21_00016-007"], "technician_name": ["Іван"]},
        )
        assert resp.status_code == 303
        order = db.scalar(select(Order).where(Order.source == "lab"))
        assert order.job_code == "2026-07-21_00016-007"
        assert order.technician_name == "Іван"
        assert cap["works"][0]["job_code"] == "2026-07-21_00016-007"
        assert cap["works"][0]["technician_name"] == "Іван"


def test_create_manual_order_multi_clients_one_push(monkeypatch):
    """Three clients in a single submit → three sheet rows + three orders,
    aligned by index; one empty trailing row is skipped."""
    engine = _database()
    cap = _stub_sheet_write(monkeypatch, note_rows=[85, 86, 87])
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="client", db=db,
            **{**_empty_form(),
               "client_name": ["Іван", "Петро", "Марія", ""],
               "material_color": ["mono a3", "Ti", "emo a2", ""],
               "quantity": ["1", "2", "3", ""]},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?source=client"
        # exactly three works passed to the writer (blank 4th row dropped)
        assert [w["client_name"] for w in cap["works"]] == ["Іван", "Петро", "Марія"]
        orders = db.scalars(
            select(Order).where(Order.source == "sheet_client").order_by(Order.row_number)
        ).all()
        assert [o.client_name for o in orders] == ["Іван", "Петро", "Марія"]
        assert [o.row_number for o in orders] == [85 - HEADER_ROWS, 86 - HEADER_ROWS, 87 - HEADER_ROWS]


def test_create_manual_order_multi_rejects_bad_row(monkeypatch):
    """A client row with a name but no material errors out and nothing is
    written — the whole batch is validated before any DB/sheet write."""
    engine = _database()
    _stub_sheet_write(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="client", db=db,
            **{**_empty_form(),
               "client_name": ["Іван", "Петро"],
               "material_color": ["mono a3", ""],  # 2nd row missing material
               "quantity": ["1", "2"]},
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert db.scalar(select(Order)) is None


def test_create_manual_order_requires_client_and_material(monkeypatch):
    engine = _database()
    _stub_sheet_write(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="client", db=db,
            **{**_empty_form(), "client_name": ["   "], "material_color": ["x"]},
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert db.scalar(select(Order)) is None  # nothing created


def test_create_manual_order_reports_missing_today_tab(monkeypatch):
    engine = _database()
    monkeypatch.setattr(web, "_recent_manual_adds", {})
    monkeypatch.setattr(web, "open_spreadsheet", lambda db=None: object())
    # No dated tab anywhere in the document -> resolver returns None.
    monkeypatch.setattr(web, "latest_worksheet_on_or_before", lambda ss, d: None)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        resp = web.create_manual_order(
            request=_request(user.id), work_type="client", db=db,
            **{**_empty_form(), "client_name": ["Басараб"],
               "material_color": ["mono a3"], "quantity": ["1"]},
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert db.scalar(select(Order)) is None


def test_create_manual_order_double_submit_is_ignored(monkeypatch):
    """An F5/back resubmit of the exact same batch by the same operator inside
    the dedup window writes NOTHING new — one order, one sheet append."""
    monkeypatch.setattr(web, "_recent_manual_adds", {})
    engine = _database()
    cap = _stub_sheet_write(monkeypatch, note_rows=[65])
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        payload = {**_empty_form(), "client_name": ["Басараб"],
                   "material_color": ["mono a3"], "quantity": ["2"]}
        first = web.create_manual_order(request=_request(user.id), work_type="client", db=db, **payload)
        second = web.create_manual_order(request=_request(user.id), work_type="client", db=db, **payload)

        assert first.status_code == second.status_code == 303
        assert cap["calls"] == 1  # sheet written once, not twice
        assert db.scalar(select(func.count()).select_from(Order).where(Order.source == "sheet_client")) == 1


def test_create_manual_order_different_payload_not_deduped(monkeypatch):
    """A genuinely different second add (another client) is not swallowed."""
    monkeypatch.setattr(web, "_recent_manual_adds", {})
    engine = _database()
    cap = _stub_sheet_write(monkeypatch)
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        web.create_manual_order(
            request=_request(user.id), work_type="client", db=db,
            **{**_empty_form(), "client_name": ["Басараб"], "material_color": ["mono a3"], "quantity": ["1"]},
        )
        web.create_manual_order(
            request=_request(user.id), work_type="client", db=db,
            **{**_empty_form(), "client_name": ["Петренко"], "material_color": ["emo a2"], "quantity": ["1"]},
        )
        assert cap["calls"] == 2
        assert db.scalar(select(func.count()).select_from(Order).where(Order.source == "sheet_client")) == 2


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

        client_only = _call_get_queue(
            db, user, monkeypatch, tmp_path, date_param="08.08.26", source="client"
        )
        assert [o.id for o in client_only["orders"]] == [email_order.id]


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

        client_only = _call_get_queue(db, user, monkeypatch, tmp_path, source="client")
        assert client_only["orders_lab"] == []
        assert [o.id for o in client_only["orders_email"]] == [email_order.id]


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


def test_restore_rejected_email_returns_to_triage():
    """A rejected letter flips straight back (відхилено -> нове); its files never
    left the spool, so nothing else needs undoing."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        rejected = EmailMessage(uid="r1", status="відхилено")
        db.add(rejected)
        db.commit()

        asyncio.run(web.restore_email(request=_request(user.id), email_id=rejected.id, db=db))
        db.refresh(rejected)
        assert rejected.status == "нове"


def test_restore_accepted_email_unwinds_order_and_files(monkeypatch):
    """Un-accepting a processed letter deletes its order, moves attachments back
    to the spool and blanks the sheet row — leaving it re-processable as "нове"
    with no duplicate."""
    engine = _database()
    # Stub the filesystem move-back and the sheet blanking so no real IO happens.
    moved = {}
    monkeypatch.setattr(
        web, "restore_attachments_to_spool",
        lambda root, uid, paths: moved.setdefault("call", (uid, paths)) or [__import__("pathlib").Path(f"/spool/{uid}/f.stl")],
    )
    monkeypatch.setattr(web, "open_spreadsheet", lambda db=None: object())
    fake_ws = SimpleNamespace(id=1, title="15.08.26")
    monkeypatch.setattr(web, "get_worksheet_by_name", lambda ss, name: fake_ws)
    cleared = {}
    monkeypatch.setattr(web, "clear_placeholder_row", lambda ws, row: cleared.setdefault("row", row))

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        order = Order(source="email", sheet_tab="15.08.26", row_number=80, status="нове")
        db.add(order)
        db.flush()
        email = EmailMessage(uid="a1", status="прийнято", order_id=order.id, attachments_status="ready")
        db.add(email)
        db.flush()
        db.add(Attachment(email_message_id=email.id, filename="f.stl", saved_path="/export/Client/нова папка/mono/f.stl"))
        db.commit()
        order_id = order.id

        asyncio.run(web.restore_email(request=_request(user.id), email_id=email.id, db=db))

        db.refresh(email)
        assert email.status == "нове"
        assert email.order_id is None
        assert db.get(Order, order_id) is None  # order deleted
        assert moved["call"][0] == "a1"  # files moved back under the uid
        assert cleared["row"] == 80 + HEADER_ROWS  # sheet placeholder blanked


def test_fetch_email_link_downloads_one_and_returns_done_row(monkeypatch, tmp_path):
    """/mail/{id}/fetch-link downloads a single whitelisted link and returns its
    row marked done, with a new Attachment created."""
    engine = _database()
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(tmp_path / "mail"))
    saved = tmp_path / "model.stl"; saved.write_bytes(b"STL")
    monkeypatch.setattr(web, "download_link", lambda link, dest, existing_names=frozenset(): saved)
    captured = {}
    monkeypatch.setattr(
        web.templates, "TemplateResponse",
        lambda request, template, context: captured.update(template=template, ctx=context) or context,
    )

    fid = "1LIyJrFNKnY7oFyMadR1W5mRgRpAW9ivl"
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="m1", status="нове",
                             body_text=f"<https://drive.google.com/file/d/{fid}/view>")
        db.add(email); db.commit()

        web.fetch_email_link(request=_request(user.id), email_id=email.id, ref=fid, db=db)

        assert captured["template"] == "_mail_link_row.html"
        assert captured["ctx"]["link_status"] == "done"
        assert captured["ctx"]["result_name"] == "model.stl"
        atts = db.query(Attachment).filter(Attachment.email_message_id == email.id).all()
        assert len(atts) == 1 and atts[0].filename == "model.stl"


def test_fetch_email_link_reports_error_row(monkeypatch, tmp_path):
    """A LinkDownloadError (e.g. file not shared) comes back as an error row, no
    attachment created."""
    engine = _database()
    monkeypatch.setattr(web, "MAIL_ATTACHMENTS_PATH", str(tmp_path / "mail"))

    def boom(link, dest, existing_names=frozenset()):
        raise web.LinkDownloadError("файл не розшарено")

    monkeypatch.setattr(web, "download_link", boom)
    captured = {}
    monkeypatch.setattr(
        web.templates, "TemplateResponse",
        lambda request, template, context: captured.update(ctx=context) or context,
    )

    fid = "104xWP_qkbzSMNXZFdf_LpI8IzSpz2anh"
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="m2", status="нове",
                             body_text=f"https://drive.google.com/open?id={fid}")
        db.add(email); db.commit()

        web.fetch_email_link(request=_request(user.id), email_id=email.id, ref=fid, db=db)
        assert captured["ctx"]["link_status"] == "error"
        assert "розшарено" in captured["ctx"]["link_message"]
        assert db.query(Attachment).count() == 0


def test_fetch_email_link_unknown_ref_is_error_row(monkeypatch, tmp_path):
    engine = _database()
    captured = {}
    monkeypatch.setattr(
        web.templates, "TemplateResponse",
        lambda request, template, context: captured.update(ctx=context) or context,
    )
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(uid="m3", status="нове", body_text="no links here")
        db.add(email); db.commit()
        web.fetch_email_link(request=_request(user.id), email_id=email.id, ref="nope", db=db)
        assert captured["ctx"]["link_status"] == "error"
