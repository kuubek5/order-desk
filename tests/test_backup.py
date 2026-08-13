
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.backup import BackupFormatError, BackupPasswordError, create_backup, restore_backup
from app.crypto import decrypt_value
from app.db import Base
from app.models import (
    AppSetting,
    Client,
    Comment,
    Order,
    ReworkRecord,
    StatusEvent,
    User,
)
from app.settings_store import set_setting


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _seed(db: Session) -> User:
    admin = User(username="admin", password_hash="hash-1", full_name="Адмін Іваненко", role="адмін")
    operator = User(username="operator", password_hash="hash-2", full_name="Оксана Гриценко", role="оператор")
    db.add_all([admin, operator])
    db.commit()

    order = Order(
        source="lab",
        sheet_tab="11.08.26",
        row_number=7,
        work_order_no="24122",
        material_color="пмма A2",
        kind="анатомія",
        quantity="6",
        status="у фрезеруванні",
        client_name="Кравчук Л.",
    )
    db.add(order)
    db.commit()

    db.add(StatusEvent(order_id=order.id, operator_id=operator.id, status="прийнято", actor="Оксана Гриценко"))
    db.add(Comment(order_id=order.id, source="operator", author="Оксана Гриценко", text="покрити опаком"))
    db.add(ReworkRecord(order_id=order.id, occurrence=2, blame="технік", redo_quantity="1"))
    db.add(Client(canonical_name="Кравчук Людмила", phone="+380501234567", email="kravchuk@ukr.net"))
    db.commit()

    set_setting(db, "google_sheet_id", "1IIEkBnPoDcxgo3-41IdbJu6FZXNawYX9UNdoekFDPbs")
    set_setting(db, "imap_password", "app-specific-password-123")
    set_setting(db, "google_service_account_json", '{"type": "service_account", "project_id": "test"}')
    db.commit()

    return admin


def test_create_backup_returns_valid_envelope():
    db = Session(_database())
    _seed(db)

    raw = create_backup(db, "correct horse battery staple")

    import json

    envelope = json.loads(raw)
    assert envelope["app"] == "order-desk"
    assert envelope["format_version"] == 1
    assert "salt" in envelope and "payload" in envelope
    # The payload must not leak the plaintext secret anywhere in the file.
    assert b"app-specific-password-123" not in raw


def test_restore_round_trips_every_table():
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "correct horse battery staple")

    fresh = Session(_database())
    counts = restore_backup(fresh, raw, "correct horse battery staple")

    assert counts["users"] == 2
    assert counts["orders"] == 1
    assert counts["status_events"] == 1
    assert counts["comments"] == 1
    assert counts["rework_records"] == 1
    assert counts["clients"] == 1
    assert counts["app_settings"] == 3

    order = fresh.query(Order).one()
    assert order.work_order_no == "24122"
    assert order.material_color == "пмма A2"

    users = {u.username: u for u in fresh.query(User).all()}
    assert users["operator"].full_name == "Оксана Гриценко"
    assert users["admin"].password_hash == "hash-1"  # hash carried over verbatim, not re-derived

    status_event = fresh.query(StatusEvent).one()
    assert status_event.order_id == order.id  # FK relationships preserved through restore
    assert status_event.actor == "Оксана Гриценко"


def test_restore_reencrypts_secrets_under_this_machine_key():
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "correct horse battery staple")

    fresh = Session(_database())
    restore_backup(fresh, raw, "correct horse battery staple")

    row = fresh.query(AppSetting).filter_by(key="imap_password").one()
    # Ciphertext in the restored DB must be decryptable by this process's
    # own live key (app.crypto), independent of the backup password.
    assert decrypt_value(row.value_encrypted) == "app-specific-password-123"

    sheet_id_row = fresh.query(AppSetting).filter_by(key="google_sheet_id").one()
    assert decrypt_value(sheet_id_row.value_encrypted) == "1IIEkBnPoDcxgo3-41IdbJu6FZXNawYX9UNdoekFDPbs"


def test_restore_wrong_password_raises_password_error():
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "correct horse battery staple")

    fresh = Session(_database())
    with pytest.raises(BackupPasswordError):
        restore_backup(fresh, raw, "wrong password entirely")


def test_restore_garbage_file_raises_format_error():
    fresh = Session(_database())
    with pytest.raises(BackupFormatError):
        restore_backup(fresh, b"not even json", "any password")

    with pytest.raises(BackupFormatError):
        restore_backup(fresh, b'{"hello": "world"}', "any password")


def test_restore_replaces_rather_than_merges():
    """A second restore into a DB that already has different data wipes it
    first — this is a recovery flow, not an accumulate-forever import."""
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "pw")

    target = Session(_database())
    target.add(User(username="stale-user", password_hash="x", full_name="Stale", role="оператор"))
    target.commit()
    assert target.query(User).count() == 1

    restore_backup(target, raw, "pw")

    usernames = {u.username for u in target.query(User).all()}
    assert usernames == {"admin", "operator"}
    assert "stale-user" not in usernames
