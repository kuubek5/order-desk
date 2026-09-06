"""Секрети й зміна ключа шифрування — інваріант «деградуємо, а не падаємо».

CLAUDE.md §14 («Секрети») називає це критичним, а тесту досі не було. Бойовий
випадок (памʼятка `project_encryption_key_drift_500`): переїзд теки даних або
нова інсталяція дають ІНШИЙ `master.key`, старі значення в `app_settings`
залишаються зашифрованими попереднім ключем — і кожне `get_setting` кидало б
`InvalidToken`. А `get_setting` кличеться майже з кожного роуту, тож один
незловлений виняток = 500 на всьому застосунку, включно з екраном, де ключ
можна було б виправити.

Тому контракт такий:
  * `decrypt_value` чесно кидає `InvalidToken` — це рівень крипто, він не має
    ховати правду;
  * `get_setting` цей виняток ловить і повертає None («не задано») — застосунок
    живий, секрет доведеться ввести заново;
  * `setting_unreadable` відрізняє «не задано» від «задано, але не читається» —
    інакше екран активації сказав би «не активовано» замість «ключ змінився».

Ключі тут ГЕНЕРУЮТЬСЯ (`Fernet.generate_key()`), а не хардкодяться: у тесті
потрібні саме два різні ключі, і жоден із них не має стосунку до бойового.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app import crypto
from app.models import AppSetting
from app.settings_store import (
    get_all_settings,
    get_setting,
    set_setting,
    setting_unreadable,
)


@pytest.fixture
def rotate_key(monkeypatch):
    """Підміняє діючий ключ шифрування на новий, згенерований.

    `app.crypto` створює `_fernet` один раз при імпорті, а `settings_store`
    кличе `encrypt_value`/`decrypt_value` — тобто читає цю глобальну змінну на
    момент виклику. Підміна саме `_fernet` (а не змінної оточення) — єдиний
    спосіб відтворити «інший master.key» без перезапуску процесу.
    """

    def _rotate() -> None:
        monkeypatch.setattr(crypto, "_fernet", Fernet(Fernet.generate_key()))

    return _rotate


def test_encrypt_decrypt_round_trip():
    """Базовий цикл: що зашифрували — те й прочитали, а шифротекст не є текстом."""
    token = crypto.encrypt_value("pa$$w0rd Ключ")
    assert token != "pa$$w0rd Ключ"
    assert crypto.decrypt_value(token) == "pa$$w0rd Ключ"


def test_decrypt_with_another_key_raises_invalid_token(rotate_key):
    """Рівень крипто мовчати НЕ повинен — інакше нікому буде відрізнити
    «ключ змінився» від «секрет порожній»."""
    token = crypto.encrypt_value("imap-secret")
    rotate_key()
    with pytest.raises(InvalidToken):
        crypto.decrypt_value(token)


def test_setting_round_trip_through_db(db_session):
    set_setting(db_session, "imap_password", "секрет-123")
    db_session.commit()
    assert get_setting(db_session, "imap_password") == "секрет-123"


def test_setting_overwrite_keeps_single_row(db_session):
    """Повторний запис оновлює рядок, а не додає другий — інакше `session.get`
    повертав би довільний із двох, і секрет «іноді старий»."""
    set_setting(db_session, "imap_login", "old@ukr.net")
    db_session.commit()
    set_setting(db_session, "imap_login", "new@ukr.net")
    db_session.commit()
    assert get_setting(db_session, "imap_login") == "new@ukr.net"
    assert db_session.query(AppSetting).filter_by(key="imap_login").count() == 1


def test_get_setting_degrades_to_none_after_key_change(db_session, rotate_key):
    """ГОЛОВНИЙ інваріант: значення від старого ключа читається як None і НЕ
    кидає — застосунок працює далі, а не віддає 500 на кожному роуті."""
    set_setting(db_session, "imap_password", "секрет-123")
    db_session.commit()
    rotate_key()

    assert get_setting(db_session, "imap_password") is None


def test_setting_unreadable_tells_apart_missing_and_undecryptable(db_session, rotate_key):
    set_setting(db_session, "license_key", "LIC-1")
    db_session.commit()
    # Поки ключ той самий — значення читається, «нечитабельним» не є.
    assert setting_unreadable(db_session, "license_key") is False
    # Відсутнє значення — це «не задано», а не «зіпсоване».
    assert setting_unreadable(db_session, "imap_password") is False

    rotate_key()
    assert setting_unreadable(db_session, "license_key") is True
    assert setting_unreadable(db_session, "imap_password") is False


def test_get_all_settings_survives_key_change(db_session, rotate_key):
    """Екран налаштувань читає ВСІ поля одним викликом. Один нечитабельний
    секрет не має завалити весь екран — саме там ключ і вводять заново."""
    set_setting(db_session, "google_sheet_id", "SHEET-1")
    set_setting(db_session, "imap_login", "op@ukr.net")
    db_session.commit()
    rotate_key()

    values = get_all_settings(db_session)
    assert values["google_sheet_id"] is None
    assert values["imap_login"] is None
    # Ключі полів на місці — шаблон малює форму, просто порожню.
    assert "license_key" in values


def test_missing_and_empty_values_are_none(db_session):
    """Ні рядка в базі, ні значення в рядку — обидва випадки тихо дають None."""
    assert get_setting(db_session, "imap_password") is None

    db_session.add(AppSetting(key="imap_login", value_encrypted=None))
    db_session.commit()
    assert get_setting(db_session, "imap_login") is None
    assert setting_unreadable(db_session, "imap_login") is False


def test_empty_string_survives_round_trip(db_session):
    """Порожній рядок — це ЗАДАНЕ порожнє значення (оператор стер поле), і воно
    має пройти шифрування без винятку; що `get_setting` віддасть саме "",
    а не None, важливо для «or DEFAULT» на місцях виклику."""
    set_setting(db_session, "imap_login", "")
    db_session.commit()
    assert get_setting(db_session, "imap_login") == ""


def test_unknown_key_is_rejected(db_session):
    """Друкарська помилка в назві ключа мусить впасти одразу, а не створити
    мовчазний рядок, який ніхто ніколи не прочитає."""
    with pytest.raises(ValueError):
        set_setting(db_session, "no_such_setting", "x")
