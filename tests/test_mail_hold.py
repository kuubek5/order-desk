"""«На уточненні» — пауза листа в CRM (власник 25.09.26).

Клієнт надіслав дубль, не надіслав файлів чи не вказав матеріал: оператор
ставить лист на паузу, адміністратори дзвонять замовнику. Лист виходить із
«Вхідних» і з усіх лічильників нових у свою вкладку; «↩» повертає. Скринька
ukr.net не змінюється — тут IMAP не кличеться взагалі.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.mail_hold import hold_label, put_on_hold, release_hold
from app.models import EmailMessage
from app.routers.deps import pending_mail_count_uncached
from tests.asgi_client import MiniClient
from tests.test_mail_bulk import _bulk, _letter, _location
from tests.test_settings_slabs_render import OPERATOR, app_db  # noqa: F401 — фікстура

pytestmark = pytest.mark.usefixtures("weekday_clock")


def _client(app):
    client = MiniClient(app)
    client.login(*OPERATOR)
    return client


def _page(app, view):
    _, _, body = _client(app).get(f"/mail?view={view}")
    return body.decode("utf-8") if isinstance(body, bytes) else body


def test_put_on_hold_reason_is_optional():
    """Причина необовʼязкова (власник 25.09.26): з меню «Перемістити» — одним
    кліком без причини, «Інше» — кліком без тексту. Невідомий ключ — відмова."""
    email = SimpleNamespace(status="нове", hold_at=None, hold_reason=None, hold_note=None, hold_by=None)
    assert put_on_hold(email, "nonsense", "", "op") == "Невідома причина"
    assert email.hold_at is None
    assert put_on_hold(email, "", "", "op") is None
    assert email.hold_reason is None and hold_label(email) == "На уточненні"
    assert put_on_hold(email, "other", "  ", "op") is None
    assert hold_label(email) == "Інше"
    assert put_on_hold(email, "dup_files", "", "op") is None
    assert email.hold_at is not None and email.hold_by == "op"
    assert hold_label(email) == "Дубль файлів"
    release_hold(email)
    assert email.hold_at is None and email.hold_reason is None


def test_accepted_letter_cannot_be_held():
    email = SimpleNamespace(status="прийнято", hold_at=None, hold_reason=None, hold_note=None, hold_by=None)
    assert put_on_hold(email, "no_files", "", "op") is not None
    assert email.hold_at is None


def test_hold_moves_letter_out_of_inbox_and_counters(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        held = _letter(db, "1", subject="Лист-на-паузі")
        _letter(db, "2", subject="Звичайний-лист")
    before = pending_mail_count_uncached()

    status, headers, _ = _client(app).post(
        f"/mail/{held}/hold", {"reason": "other", "note": "який колір?"},
        {"referer": "http://127.0.0.1:8000/mail"},
    )
    assert status == 303

    with session_factory() as db:
        row = db.get(EmailMessage, held)
        assert row.hold_at is not None
        assert row.hold_reason == "other" and row.hold_note == "який колір?"
        assert row.status == "нове"          # стан CRM, не статус листа
        assert row.mailbox_folder is None    # скринька не змінюється
    assert pending_mail_count_uncached() == before - 1

    inbox = _page(app, "pending")
    assert "Лист-на-паузі" not in inbox and "Звичайний-лист" in inbox
    hold = _page(app, "hold")
    assert "Лист-на-паузі" in hold and "Звичайний-лист" not in hold
    assert "який колір?" in hold


def test_unhold_returns_letter_to_inbox(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        held = _letter(db, "1", subject="Лист-на-паузі")
        email = db.get(EmailMessage, held)
        put_on_hold(email, "no_files", "", "op")
        db.commit()

    status, _, body = _client(app).post(
        f"/mail/{held}/unhold", {}, {"HX-Request": "true"},
    )
    assert status == 200 and not body  # рядок вкладки видаляє сам htmx

    with session_factory() as db:
        assert db.get(EmailMessage, held).hold_at is None
    assert "Лист-на-паузі" in _page(app, "pending")


def test_bulk_unhold_and_reject_clear_the_hold(app_db):  # noqa: F811
    app, session_factory = app_db
    with session_factory() as db:
        ids = [_letter(db, str(n)) for n in range(3)]
        for i in ids:
            put_on_hold(db.get(EmailMessage, i), "no_material", "", "op")
        db.commit()

    assert _location(_bulk(app, "unhold", ids[:2], view="hold")) == "/mail?view=hold"
    _bulk(app, "reject", ids[2:], view="hold")
    with session_factory() as db:
        assert db.get(EmailMessage, ids[0]).hold_at is None
        assert db.get(EmailMessage, ids[1]).hold_at is None
        rejected = db.get(EmailMessage, ids[2])
        assert rejected.status == "відхилено" and rejected.hold_at is None


def test_bulk_hold_from_move_menu(app_db):  # noqa: F811
    """«На уточненні» в меню «Перемістити» (власник 25.09.26): масова пауза;
    прийнятий лист не чіпається."""
    app, session_factory = app_db
    with session_factory() as db:
        a = _letter(db, "1")
        b = _letter(db, "2")
        accepted = _letter(db, "3", status="прийнято")
    client = MiniClient(app)
    client.login(*OPERATOR)
    result = client.post(
        "/mail/bulk",
        {"action": "hold", "ids": f"{a},{b},{accepted}", "reason": "dup_files", "note": ""},
        {"referer": "http://127.0.0.1:8000/mail"},
    )
    assert _location(result) == "/mail"
    with session_factory() as db:
        assert db.get(EmailMessage, a).hold_reason == "dup_files"
        assert db.get(EmailMessage, b).hold_at is not None
        assert db.get(EmailMessage, accepted).hold_at is None

    # Пункт-папка «На уточненні» в меню — без причини, одним кліком.
    with session_factory() as db:
        c = _letter(db, "4")
    client.post("/mail/bulk", {"action": "hold", "ids": str(c), "reason": "", "note": ""})
    with session_factory() as db:
        row = db.get(EmailMessage, c)
        assert row.hold_at is not None and row.hold_reason is None
