"""«Прийняти як переробку» — лист, повернутий у Вхідні на переробку (03.11.26).

Бойовий випадок: лист 2319 (Oleksandr) адміністратори повернули у «Вхідні» з
«Відфрезеровано» — роботу вже видали, клієнт просить переробити. CRM показувала
«↺ повернуто», але зробити з листом не давала нічого: прийняти вдруге не можна
(статус «прийнято», файли вже прив'язані), а «Відкотити прийняття» правильно
відмовляло — і радило видалити першу роботу, тобто знищити історію першого
фрезерування.

Рішення власника: переробка з пошти — НОВА звичайна робота в черзі дня, у
загальну кількість одиниць, без винуватця й без колонок БРАК; файли першої
роботи КОПІЮЮТЬСЯ в теку сьогоднішнього дня; перша робота не змінюється ніяк.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import sheets as sheets_mod
from app.business_day import business_today
from app.models import ActionLog, Attachment, EmailMessage, Order, StatusEvent, SyncLog
from app.parser import HEADER_ROWS
from app.routers import mail as mail_router_mod
from app.services import mail_accept as mail_accept_svc
from app.services import mail_rework as mail_rework_svc
from test_mail_transaction_safety import _accept, _database, _letter, _request, _user, _wire


def _shop(monkeypatch, tmp_path):
    engine = _database()
    export_root, mail_root = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(mail_rework_svc, "get_export_folder_path", lambda _db: str(export_root))
    return engine, export_root, mail_root


def _returned(db: Session, email: EmailMessage, status: str = "видано") -> Order:
    """Перша робота вже пішла далі, лист повернули у Вхідні прямо в пошті."""
    first = db.scalar(select(Order).where(Order.source_email_id == email.id))
    first.status = status
    first.quantity = "3"
    first.opak_units = 2
    first.kind = "анатомія"
    email.reopened_at = datetime(2026, 11, 3, 10, 50)
    db.commit()
    return first


def _rework(db, user, email, **fields):
    values = dict(
        source_order_id="", client_name="Люмі-Дент", material_color="моно а3",
        quantity="3", opak="", sum3d_id="", accept_anyway="",
    )
    values.update(fields)
    return mail_router_mod.accept_email_rework(
        request=_request(user.id), email_id=email.id, db=db, **values,
    )


def _ok(response) -> None:
    assert response.status_code == 303
    assert "error=" not in response.headers["location"], response.headers["location"]


def _error(response) -> str:
    assert response.status_code == 303
    location = response.headers["location"]
    assert "error=" in location, location
    from urllib.parse import unquote
    return unquote(location.split("error=", 1)[1])


def _snapshot(order: Order) -> dict:
    return {
        name: getattr(order, name)
        for name in (
            "status", "quantity", "client_name", "material_color", "sheet_tab",
            "row_number", "export_folder_path", "cam_comment", "opak_units",
            "sum3d_id", "mill_count", "archived_at",
        )
    }


def test_returned_letter_becomes_a_new_rework_order(tmp_path, monkeypatch, weekday_clock):
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        first = _returned(db, email)
        first_id = first.id
        first_before = _snapshot(first)
        first_events = len(first.status_events)
        first_file = Path(db.scalar(select(Attachment)).saved_path)
        assert first_file.is_file()

        _ok(_rework(db, user, email))

    with Session(engine) as db:
        orders = db.scalars(select(Order).order_by(Order.id)).all()
        assert len(orders) == 2
        first, rework = orders
        # Перша робота — рівно та сама: статус, рядок, тека, історія.
        assert first.id == first_id
        assert _snapshot(first) == first_before
        assert len(first.status_events) == first_events

        # Нова звичайна поштова робота того самого листа, у загальну кількість:
        # без «Який раз» (виробіток не рахує mill_count ≥ 2) і без брака.
        assert rework.source == "email"
        assert rework.source_email_id == email.id
        assert rework.status == "нове"
        assert rework.client_name == "Люмі-Дент"
        assert rework.material_color == "моно а3"
        assert rework.quantity == "3"
        assert rework.kind == "анатомія"
        assert rework.mill_count is None
        assert rework.rework_records == []
        assert rework.cam_comment == "переробка"
        assert rework.sum3d_id is None
        # Рядок у таблиці чекає повтор (таблиця в тесті недоступна).
        assert rework.sheet_row_pending is not None
        assert rework.sheet_tab == business_today().strftime("%d.%m.%y")

        # Файли першої роботи СКОПІЙОВАНО в теку сьогодні; повтор матеріалу
        # за день — підпапка « (2)». Оригінал на місці, вкладення за першою.
        today = business_today().strftime("%d.%m.%y")
        assert rework.export_folder_path == f"Люмі-Дент/{today}/моно а3 (2)"
        copy = export_root / "Люмі-Дент" / today / "моно а3 (2)" / "crown.stl"
        assert copy.read_bytes() == b"STL"
        assert first_file.read_bytes() == b"STL"
        attachment = db.scalar(select(Attachment))
        assert attachment.order_id == first_id
        assert Path(attachment.saved_path) == first_file

        # Лист відпрацював повернення: лишається прийнятим, зникає з «Вхідних».
        stored = db.get(EmailMessage, email.id)
        assert stored.status == "прийнято"
        assert stored.reopened_at is None
        assert stored.order_id == first_id

        # Хто що зробив: подія статусу з позначкою й рядок журналу дій.
        event = db.scalar(select(StatusEvent).where(StatusEvent.order_id == rework.id))
        assert event.actor == "operator"
        assert "переробка" in (event.note or "")
        assert f"#{first_id}" in (event.note or "")
        mark = db.scalar(select(ActionLog).where(ActionLog.order_id == rework.id))
        assert mark.field == mail_rework_svc.REWORK_FIELD
        assert mark.new_value == str(first_id)
        assert db.scalar(
            select(SyncLog).where(SyncLog.message.like(f"email {email.id}: переробка%"))
        ) is not None


def test_rework_on_a_later_day_lands_in_todays_folder(tmp_path, monkeypatch, weekday_clock):
    """Перша робота лежить у теці свого дня — копія йде в теку СЬОГОДНІ, у
    теку того самого клієнта (з першої роботи), навіть якщо ім'я в таблиці
    записане інакше."""
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)
    old_dir = export_root / "Lumi Dent clinic" / "01.10.26" / "моно а3"
    old_dir.mkdir(parents=True)
    (old_dir / "crown.stl").write_bytes(b"OLD")

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email = EmailMessage(
            uid="u1", status="прийнято", from_address="lumi@ukr.net",
            attachments_status="ready", reopened_at=datetime(2026, 11, 3, 9, 0),
        )
        db.add(email)
        db.flush()
        first = Order(
            source="email", source_email_id=email.id, sheet_tab="01.10.26",
            client_name="Люмі-Дент", material_color="моно а3", quantity="2",
            status="видано", export_folder_path="Lumi Dent clinic/01.10.26/моно а3",
        )
        db.add(first)
        db.flush()
        email.order_id = first.id
        db.add(Attachment(
            email_message_id=email.id, order_id=first.id, filename="crown.stl",
            saved_path=str(old_dir / "crown.stl"),
        ))
        db.commit()

        _ok(_rework(db, user, email, quantity="2"))

    today = business_today().strftime("%d.%m.%y")
    with Session(engine) as db:
        rework = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert rework.export_folder_path == f"Lumi Dent clinic/{today}/моно а3"
    assert (export_root / "Lumi Dent clinic" / today / "моно а3" / "crown.stl").read_bytes() == b"OLD"
    assert (old_dir / "crown.stl").read_bytes() == b"OLD"


def test_rework_needs_a_letter_returned_to_the_inbox(tmp_path, monkeypatch):
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        # Новий лист — це звичайне прийняття, не переробка.
        assert "повернуто" in _error(_rework(db, user, email))
        _ok(_accept(db, user, email))
        # Прийнятий, але НЕ повернутий у Вхідні — теж ні.
        assert "повернуто" in _error(_rework(db, user, email))

    with Session(engine) as db:
        assert len(db.scalars(select(Order)).all()) == 1


def test_rework_takes_edited_quantity_and_opak(tmp_path, monkeypatch, weekday_clock):
    """Переробляють частину — кількість правиться; опак іде в коментар для CAM
    поруч із позначкою, як у прийнятті."""
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        _returned(db, email)
        _ok(_rework(db, user, email, quantity="1", opak="1"))

    with Session(engine) as db:
        rework = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert rework.quantity == "1"
        assert rework.cam_comment == "переробка, 1 opaq"
        assert rework.opak_units == 1


def test_rework_form_is_prefilled_from_the_first_order(tmp_path, monkeypatch, weekday_clock):
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        first = _returned(db, email)

        context = mail_router_mod._mail_panel_context(db, email, user)
        form = context["rework"]
        assert form is not None
        assert form["source"].id == first.id
        assert form["client_name"] == "Люмі-Дент"
        assert form["material_color"] == "моно а3"
        assert form["quantity"] == "3"
        assert form["opak"] == "2"
        assert form["sum3d_id"] == ""
        assert form["files"] == 1
        assert form["missing"] == 0
        today = business_today().strftime("%d.%m.%y")
        assert form["target"] == f"Люмі-Дент/{today}/моно а3 (2)"

        # Звичайний прийнятий лист (не повернутий) форми переробки не має.
        email.reopened_at = None
        db.commit()
        assert mail_router_mod._mail_panel_context(db, email, user)["rework"] is None


def test_missing_first_files_need_a_conscious_yes(tmp_path, monkeypatch, weekday_clock):
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        _returned(db, email)
        Path(db.scalar(select(Attachment)).saved_path).unlink()

        message = _error(_rework(db, user, email))
        assert "немає на диску" in message
        assert len(db.scalars(select(Order)).all()) == 1
        assert db.get(EmailMessage, email.id).reopened_at is not None

        _ok(_rework(db, user, email, accept_anyway="1"))

    with Session(engine) as db:
        rework = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert rework.cam_comment == "переробка"
        assert rework.export_folder_path is None


def test_failed_commit_removes_the_copies(tmp_path, monkeypatch, weekday_clock):
    """Копії лягли на диск ДО коміту — невдалий коміт мусить їх прибрати, а
    лист лишити повернутим (переробку можна створити ще раз)."""
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        _returned(db, email)
        first_file = Path(db.scalar(select(Attachment)).saved_path)

        def failing_commit():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "commit", failing_commit)
        assert "database is locked" in _error(_rework(db, user, email))

    today = business_today().strftime("%d.%m.%y")
    assert not (export_root / "Люмі-Дент" / today / "моно а3 (2)").exists()
    assert first_file.read_bytes() == b"STL"
    with Session(engine) as db:
        assert len(db.scalars(select(Order)).all()) == 1
        assert db.get(EmailMessage, email.id).reopened_at is not None


def test_second_return_reworks_the_first_order_again(tmp_path, monkeypatch, weekday_clock):
    """Лист повернули вдруге — джерело знову перша робота (саме вона тримає
    файли листа), а не попередня переробка."""
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        first = _returned(db, email)
        _ok(_rework(db, user, email))
        email.reopened_at = datetime(2026, 11, 3, 15, 0)
        db.commit()

        form = mail_router_mod._mail_panel_context(db, email, user)["rework"]
        assert [o.id for o in form["sources"]] == [first.id]
        _ok(_rework(db, user, email))

    today = business_today().strftime("%d.%m.%y")
    with Session(engine) as db:
        assert len(db.scalars(select(Order)).all()) == 3
        latest = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert latest.export_folder_path == f"Люмі-Дент/{today}/моно а3 (3)"
    assert (export_root / "Люмі-Дент" / today / "моно а3 (3)" / "crown.stl").is_file()


def test_rework_writes_its_sheet_row_with_the_mark(tmp_path, monkeypatch, weekday_clock):
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        _returned(db, email)

        calls = []

        def _fake_append(worksheet, client_name, quantity, material_color, **kwargs):
            calls.append((client_name, quantity, material_color, kwargs))
            return 70

        monkeypatch.setattr(mail_accept_svc, "append_mail_placeholder_row", _fake_append)
        monkeypatch.setattr(mail_accept_svc, "open_spreadsheet", lambda db=None: object())
        monkeypatch.setattr(
            mail_accept_svc, "latest_worksheet_on_or_before",
            lambda *args, **kwargs: SimpleNamespace(title="03.11.26"),
        )
        _ok(_rework(db, user, email, quantity="1", sum3d_id="15-20-11"))

    assert len(calls) == 1
    client, quantity, material, kwargs = calls[0]
    assert (client, quantity, material) == ("Люмі-Дент", "1", "моно а3")
    assert kwargs["cam_comment"] == "переробка"
    assert kwargs["sum3d_id"] == "15-20-11"
    with Session(engine) as db:
        rework = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert rework.sheet_tab == "03.11.26"
        assert rework.row_number == 70 - HEADER_ROWS
        assert rework.sheet_row_pending is None


def test_rework_without_google_queues_its_row(tmp_path, monkeypatch, weekday_clock):
    """Немає зв'язку з Google (фікс 03.11.26) — переробка не чекає таблицю:
    робота лягає у вкладку з бази, рядок допише повтор."""
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        _returned(db, email)
        calls = []
        monkeypatch.setattr(
            mail_accept_svc, "append_mail_placeholder_row", lambda *a, **kw: calls.append(a) or 70
        )
        monkeypatch.setattr(sheets_mod, "google_unreachable", lambda: True)
        _ok(_rework(db, user, email))

    assert calls == []
    with Session(engine) as db:
        rework = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert rework.row_number is None
        assert rework.sheet_row_pending is not None
        assert db.scalar(
            select(SyncLog).where(SyncLog.message.like("%немає зв'язку з Google%"))
        ) is not None


def test_unaccept_of_a_returned_letter_points_to_rework(tmp_path, monkeypatch):
    """Підказка «видаліть її з черги в паспорті» для поверненого листа шкідлива:
    вона знищує історію першого фрезерування."""
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        _returned(db, email)
        response = mail_router_mod.restore_email(request=_request(user.id), email_id=email.id, db=db)

    message = _error(response)
    assert "Прийняти як переробку" in message
    assert "видаліть" not in message


def test_unaccept_refuses_a_letter_with_a_rework(tmp_path, monkeypatch, weekday_clock):
    """Відкат прибрав би обидві роботи, а копії переробки лишились би в export
    нічиїми — тому лист із переробкою не відкочується."""
    engine, export_root, mail_root = _shop(monkeypatch, tmp_path)

    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        email, _stl = _letter(db, mail_root / "u1")
        _ok(_accept(db, user, email))
        _returned(db, email, status="прораховано")
        _ok(_rework(db, user, email))
        response = mail_router_mod.restore_email(request=_request(user.id), email_id=email.id, db=db)

    assert "переробк" in _error(response)
    with Session(engine) as db:
        assert len(db.scalars(select(Order)).all()) == 2
        assert db.get(EmailMessage, email.id).status == "прийнято"
