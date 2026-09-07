"""Часткова копія: повернути одне, не зачепивши решти.

Головний ризик тут не «не відновилось», а «відновилось ЗАЙВЕ»: людина хотіла
повернути видаленого оператора, а разом із ним відкотились роботи за день.
Тому майже всі перевірки нижче — про те, чого статися НЕ мало.
"""

import base64
import json
from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.backup import _derive_key, create_backup, restore_backup
from app.backup_parts import PARTS, PART_BY_KEY, parts_for_tables, tables_for
from app.crypto import decrypt_value
from app.db import Base
from app.models import (
    AppSetting,
    Client,
    Furnace,
    Material,
    Order,
    User,
)
from app.settings_store import set_setting


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _seed(db: Session) -> None:
    db.add_all([
        User(username="admin", password_hash="h1", full_name="Адмін", role="адмін"),
        User(username="oksana", password_hash="h2", full_name="Оксана", role="оператор"),
    ])
    db.add(Client(canonical_name="Кравчук Людмила", phone="+380501234567"))
    db.add(Material(name="цирконій"))
    db.add(Furnace(
        name="Піч 1", host="192.168.1.11", created_at=datetime(2026, 9, 7, 21, 0)
    ))
    db.add(Order(
        source="lab", sheet_tab="07.09.26", row_number=7, work_order_no="24122",
        material_color="пмма A2", status="у фрезеруванні",
    ))
    db.commit()
    set_setting(db, "google_sheet_id", "1IIEkBnPoDcxgo3")
    set_setting(db, "imap_password", "app-password-123")
    db.commit()


def _payload(raw: bytes, password: str) -> dict:
    envelope = json.loads(raw)
    key = _derive_key(password, base64.b64decode(envelope["salt"]))
    return json.loads(Fernet(key).decrypt(envelope["payload"].encode("ascii")))


def test_a_partial_backup_carries_only_what_was_asked_for():
    db = Session(_database())
    _seed(db)

    raw = create_backup(db, "pw-12345678", only_tables=tables_for(["users"]))
    envelope = json.loads(raw)
    payload = _payload(raw, "pw-12345678")

    assert envelope["partial"] is True
    assert set(payload["tables"]) == {"users"}
    assert "orders" not in envelope["manifest"]
    # Секрети в часткову копію не їдуть: це «поверни операторів», а не переїзд.
    assert payload["settings"] == {}
    assert b"app-password-123" not in raw


def test_restoring_operators_does_not_touch_the_orders():
    """Найважливіше в усьому файлі. Повна копія заміщає все — і саме тому її не
    можна застосувати заради одного видаленого оператора."""
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "pw-12345678", only_tables=tables_for(["users"]))

    # Життя пішло далі: оператора видалили, зʼявилась нова робота.
    db.delete(db.query(User).filter_by(username="oksana").one())
    db.add(Order(
        source="lab", sheet_tab="08.09.26", row_number=9, work_order_no="24500",
        material_color="моно А3", status="нове",
    ))
    db.commit()
    assert db.query(User).count() == 1
    assert db.query(Order).count() == 2

    counts = restore_backup(db, raw, "pw-12345678")

    assert counts == {"users": 2}                         # заміщено тільки це
    assert db.query(User).count() == 2                    # оператор повернувся
    assert db.query(Order).count() == 2                   # обидві роботи на місці
    assert {o.work_order_no for o in db.query(Order)} == {"24122", "24500"}
    # І налаштування не постраждали — часткова копія їх не несла.
    assert decrypt_value(
        db.query(AppSetting).filter_by(key="imap_password").one().value_encrypted
    ) == "app-password-123"


def test_a_device_part_brings_back_passwords_too():
    """Пічки й верстати без паролів довелося б заводити руками — тобто набір,
    який не рятує від тієї самої роботи, заради якої його роблять."""
    from app.crypto import encrypt_value

    db = Session(_database())
    _seed(db)
    furnace = db.query(Furnace).one()
    furnace.password_encrypted = encrypt_value("піч-пароль")
    db.commit()

    raw = create_backup(db, "pw-12345678", only_tables=tables_for(["devices"]))
    db.delete(db.query(Furnace).one())
    db.commit()
    assert db.query(Furnace).count() == 0

    restore_backup(db, raw, "pw-12345678")

    restored = db.query(Furnace).one()
    assert decrypt_value(restored.password_encrypted) == "піч-пароль"


def test_several_parts_at_once():
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "pw-12345678", only_tables=tables_for(["users", "clients"]))

    db.query(Client).delete()
    db.delete(db.query(User).filter_by(username="oksana").one())
    db.commit()

    counts = restore_backup(db, raw, "pw-12345678")

    assert db.query(User).count() == 2
    assert db.query(Client).count() == 1
    assert db.query(Order).count() == 1                   # робота недоторкана
    assert set(counts) == {"users", "clients", "client_name_aliases", "client_sender_memory"}


def test_a_full_backup_still_replaces_everything():
    """Часткова гілка не має тихо змінити поведінку повної копії."""
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "pw-12345678")

    db.add(Order(
        source="lab", sheet_tab="09.09.26", row_number=11, work_order_no="99999",
        material_color="титан", status="нове",
    ))
    db.commit()

    restore_backup(db, raw, "pw-12345678")

    assert {o.work_order_no for o in db.query(Order)} == {"24122"}   # зайве стерто
    assert json.loads(create_backup(db, "pw-12345678").decode("utf-8"))["partial"] is False


def test_unknown_part_key_is_ignored_not_fatal():
    """Перелік приходить із браузера; чужий рядок у ньому не має валити
    збереження — набори, які ми впізнали, мусять зберегтись."""
    assert tables_for(["users", "вигадка"]) == ["users"]
    assert tables_for(["вигадка"]) == []


def test_every_part_names_real_tables():
    """Набір, який згадує неіснуючу таблицю, мовчки збереже порожнечу."""
    real = {model.__tablename__ for model in Base.__subclasses__()}
    for part in PARTS:
        unknown = set(part.tables) - real
        assert not unknown, f"набір «{part.label}» називає неіснуючі таблиці: {unknown}"
        assert part.label and part.hint, f"набір {part.key} без назви або пояснення"


def test_parts_are_recognised_back_from_the_file():
    """На боці відновлення у файлі лише імена таблиць, а людині треба сказати
    «оператори», а не «users»."""
    tables = tables_for(["users", "materials"])
    labels = {part.key for part in parts_for_tables(tables)}
    assert labels == {"users", "materials"}
    assert PART_BY_KEY["users"].label == "Оператори"


def test_orders_are_deliberately_not_a_part():
    """«Повернути тільки роботи» — це і є повна копія: роботи тягнуть за собою
    листи, вкладення, статуси, коментарі й переробки. Набір, який вдає, що
    вміє менше, ніж робить, гірший за його відсутність."""
    all_tables = {table for part in PARTS for table in part.tables}
    assert "orders" not in all_tables
    assert "status_events" not in all_tables


@pytest.mark.parametrize("key", [part.key for part in PARTS])
def test_each_part_restores_on_its_own(key):
    """Кожен набір має відновлюватись САМ — без сусідів і без повної копії."""
    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "pw-12345678", only_tables=tables_for([key]))

    counts = restore_backup(db, raw, "pw-12345678")

    assert set(counts) == set(tables_for([key]))
    assert db.query(Order).count() == 1                   # роботи цілі завжди
