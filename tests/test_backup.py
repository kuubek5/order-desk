
import base64
import json
from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.backup import (
    BackupFormatError,
    BackupPasswordError,
    _derive_key,
    create_backup,
    restore_backup,
)
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
    # 3 — паролі пічок і верстатів їдуть у копії відкрито (сам файл під
    # паролем), щоб на новому ПК вони справді працювали; старіші файли
    # читаються як були.
    assert envelope["format_version"] == 3
    assert "salt" in envelope and "payload" in envelope
    # Перелічник видно ДО пароля: екран відновлення показує, що всередині.
    assert envelope["manifest"]["orders"] >= 1
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


def test_restore_refuses_a_backup_from_a_newer_format():
    """Копія з майбутньої версії може нести таблиці, яких ця збірка не знає —
    мовчки відновити її означало б тихо їх викинути (ревʼю 07.09.26)."""
    import json

    from app.backup import BackupFormatError

    db = Session(_database())
    _seed(db)
    envelope = json.loads(create_backup(db, "pw-12345678"))
    envelope["format_version"] = 99
    with pytest.raises(BackupFormatError, match="новішою версією"):
        restore_backup(db, json.dumps(envelope).encode("utf-8"), "pw-12345678")


def test_restore_aborts_when_the_result_does_not_match_the_manifest():
    """Копія несе власний перелік «скільки рядків у якій таблиці». Якщо після
    вставки числа не збіглись — відкат, а не напівжива база."""
    import json

    from app.backup import BackupIncompleteError

    db = Session(_database())
    _seed(db)
    raw = create_backup(db, "pw-12345678")
    envelope = json.loads(raw)

    # Підмінюємо перелічник усередині payload: імітуємо втрату рядків.
    from app.backup import _derive_key
    from cryptography.fernet import Fernet
    import base64

    key = _derive_key("pw-12345678", base64.b64decode(envelope["salt"]))
    payload = json.loads(Fernet(key).decrypt(envelope["payload"].encode("ascii")))
    payload["manifest"]["orders"] = payload["manifest"]["orders"] + 5
    envelope["payload"] = Fernet(key).encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")

    before = db.query(Order).count()
    with pytest.raises(BackupIncompleteError, match="orders"):
        restore_backup(db, json.dumps(envelope).encode("utf-8"), "pw-12345678")
    db.rollback()
    assert db.query(Order).count() == before


def _database_with_foreign_keys():
    """Та сама база, але з увімкненою перевіркою зовнішніх ключів.

    Прод із K.5 працює саме так (`app/db.py::_configure_sqlite_connection`), а
    решта тестів тут — без неї, тож цикл `orders ↔ email_messages` лишався
    невидимим до живого прогону 07.09.26.
    """
    from sqlalchemy import event

    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # pragma: no cover - тривіальний хук
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(engine)
    return engine


def test_restore_survives_the_orders_email_cycle_with_foreign_keys_on():
    """`orders.email_message_id` і `email_messages.order_id` дивляться одна на
    одну — при перевірці по рядку жоден порядок вставки не проходить."""
    from app.models import EmailMessage

    db = Session(_database_with_foreign_keys())
    _seed(db)
    order = db.query(Order).first()
    letter = EmailMessage(
        uid="1",
        uid_validity="2",
        from_address="client@ukr.net",
        subject="моно а3",
        status="прийнято",
        order_id=order.id,
    )
    db.add(letter)
    db.commit()
    order.source_email_id = letter.id
    db.commit()

    raw = create_backup(db, "pw-12345678")
    restore_backup(db, raw, "pw-12345678")

    assert db.query(EmailMessage).count() == 1
    assert db.query(EmailMessage).first().order_id == order.id
    assert db.query(Order).first().source_email_id == letter.id


def test_a_setting_this_machine_cannot_decrypt_does_not_kill_the_backup():
    """Ключ шифрування прив'язаний до машини, і після переїзду теки старі рядки
    перестають читатись — рівно тоді, коли копію й роблять, щоб перевезти дані.

    Досі перше ж таке налаштування давало 500, і людина лишалась без жодного
    способу забрати роботи. Знайдено живим прогоном на стенді 07.09.26.
    """
    import json

    from cryptography.fernet import Fernet

    db = Session(_database())
    _seed(db)

    # Рядок, зашифрований ЧУЖИМ ключем: на цій машині він нечитабельний.
    broken = db.query(AppSetting).filter(AppSetting.key == "imap_password").one()
    broken.value_encrypted = Fernet(Fernet.generate_key()).encrypt(b"secret").decode("ascii")
    db.commit()

    raw = create_backup(db, "pw-12345678")
    envelope = json.loads(raw)

    # Копія зроблена, роботи в ній усі.
    assert envelope["manifest"]["orders"] == db.query(Order).count()
    # Ім'я нечитабельного налаштування видно ДО пароля — щоб на новому ПК було
    # ясно, що саме доведеться ввести руками. Значення, звісно, немає.
    assert envelope["unreadable_settings"] == ["imap_password"]
    assert "secret" not in raw.decode("utf-8")

    # І така копія відновлюється: решта секретів на місці.
    restore_backup(db, raw, "pw-12345678")
    assert decrypt_value(
        db.query(AppSetting).filter(AppSetting.key == "google_sheet_id").one().value_encrypted
    ) == "1IIEkBnPoDcxgo3-41IdbJu6FZXNawYX9UNdoekFDPbs"


def test_device_passwords_survive_a_move_to_another_machine():
    """Пароль печі й токен агента лежать колонками таблиць, зашифровані ключем
    МАШИНИ. У копії формату 2 вони їхали як є — і на новому ПК не читались:
    пічки з верстатами довелося б налаштовувати заново, тобто рівно те, від
    чого копія й мала рятувати (знайдено прогоном переїзду 07.09.26).
    """
    import json

    from app.crypto import encrypt_value
    from app.models import Furnace, Machine

    db = Session(_database())
    _seed(db)
    now = datetime(2026, 9, 7, 21, 0, 0)
    db.add(Furnace(
        name="Піч 1", host="192.168.1.11",
        password_encrypted=encrypt_value("піч-пароль"), created_at=now,
    ))
    db.add(Machine(
        name="350i", host="192.168.1.85",
        password_encrypted=encrypt_value("верстат-пароль"),
        agent_token_encrypted=encrypt_value("токен-агента"), created_at=now,
    ))
    db.commit()

    raw = create_backup(db, "pw-12345678")

    # Усередині копії — не ciphertext старої машини: інакше на новому ПК він
    # мертвий. Сам файл зашифрований паролем, тож секрет назовні не витікає.
    assert b"\xd0\xbf\xd1\x96\xd1\x87-\xd0\xbf\xd0\xb0\xd1\x80\xd0\xbe\xd0\xbb\xd1\x8c" not in raw
    payload = json.loads(
        Fernet(
            _derive_key("pw-12345678", base64.b64decode(json.loads(raw)["salt"]))
        ).decrypt(json.loads(raw)["payload"].encode("ascii"))
    )
    assert payload["tables"]["furnaces"][0]["password_encrypted"] == "піч-пароль"
    assert payload["tables"]["machines"][0]["agent_token_encrypted"] == "токен-агента"

    # А після відновлення вони знову зашифровані — ключем ЦІЄЇ машини.
    restore_backup(db, raw, "pw-12345678")
    furnace = db.query(Furnace).one()
    machine = db.query(Machine).one()
    assert furnace.password_encrypted != "піч-пароль"          # у базі не відкрито
    assert decrypt_value(furnace.password_encrypted) == "піч-пароль"
    assert decrypt_value(machine.password_encrypted) == "верстат-пароль"
    assert decrypt_value(machine.agent_token_encrypted) == "токен-агента"


def _strip_table(raw: bytes, password: str, table: str) -> bytes:
    """Зробити з копії таку, якою її записала б ПОПЕРЕДНЯ збірка: без нової таблиці.

    Розпаковуємо, викидаємо таблицю з даних і з перелічника, пакуємо назад тим
    самим ключем. Це найчесніший спосіб відтворити файл, що вже лежить у Роми
    на диску, не тримаючи в репозиторії бінарного артефакту.
    """
    envelope = json.loads(raw)
    key = _derive_key(password, base64.b64decode(envelope["salt"]))
    data = json.loads(Fernet(key).decrypt(envelope["payload"].encode("ascii")))
    data["tables"].pop(table, None)
    envelope["manifest"].pop(table, None)
    data["manifest"] = envelope["manifest"]
    envelope["payload"] = Fernet(key).encrypt(json.dumps(data).encode("utf-8")).decode("ascii")
    return json.dumps(envelope).encode("utf-8")


def test_a_full_backup_from_an_older_build_still_restores_the_secrets():
    """Копія, зроблена ДО появи нової таблиці, лишається ПОВНОЮ.

    Частковість визначалась порівнянням складу таблиць із нинішнім списком
    моделей. Варто було додати таблицю (12.09.26 — `screen_puzzles`), як КОЖНА
    вже зроблена повна копія ставала «частковою», а гілка повного відновлення
    (та, що переписує `app_settings`) мовчки пропускалась: переїзд на новий ПК
    привозив роботи без жодного секрета. Тепер віримо прапорцю `partial` у
    самому файлі.
    """
    engine = _database()
    with Session(engine) as db:
        _seed(db)
        set_setting(db, "imap_login", "phantom@ukr.net")
        raw = create_backup(db, "pw-12345678")

    older = _strip_table(raw, "pw-12345678", "screen_puzzles")
    assert json.loads(older)["partial"] is False

    fresh = _database()
    with Session(fresh) as db:
        db.add(AppSetting(key="imap_login", value_encrypted="чуже значення"))
        db.commit()
        counts = restore_backup(db, older, "pw-12345678")

        assert "app_settings" in counts, "повне відновлення пропустило секрети"
        restored = db.scalar(select(AppSetting).where(AppSetting.key == "imap_login"))
        assert restored is not None
        assert decrypt_value(restored.value_encrypted) == "phantom@ukr.net"
