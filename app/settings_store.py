"""Admin-configurable settings, stored encrypted in the DB instead of .env.

Only the DB encryption key itself stays in .env (CLAUDE.md section 7) — every
other secret (IMAP password, service-account JSON, ...) lives here so it can
be changed from the Налаштування screen without editing files on disk.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.config import EXPORT_FOLDER_PATH, GOOGLE_SHEET_ID
from app.crypto import decrypt_value, encrypt_value
from app.models import AppSetting


@dataclass
class SettingField:
    key: str
    label: str
    secret: bool = False
    multiline: bool = False
    help_text: str = ""
    # Everything defaults admin-only (Google Sheets/IMAP credentials, sync).
    # Only the two filesystem paths are operator-editable — they're a
    # per-machine detail ("which drive is the export folder mounted on
    # today"), not a credential, and whoever is actually at the workstation
    # needs to fix a moved/renamed folder without waiting on an admin.
    operator_editable: bool = False


SETTING_FIELDS = [
    SettingField(
        key="google_sheet_id",
        label="Google Sheet ID",
        help_text="ID таблиці з адресного рядка Google Sheets",
    ),
    SettingField(
        key="google_service_account_json",
        label="Google Service Account JSON",
        secret=True,
        multiline=True,
        help_text="Вміст JSON-ключа сервісного акаунта Google",
    ),
    SettingField(key="imap_login", label="Логін пошти (IMAP)"),
    SettingField(
        key="imap_password",
        label="Пароль пошти (пароль для програм)",
        secret=True,
    ),
    SettingField(
        key="export_folder_path",
        label="Шлях до папки export",
        help_text="Готові роботи для клієнтів з пошти",
        operator_editable=True,
    ),
    SettingField(
        key="technician_files_path",
        label="Шлях до папки робіт техніків",
        help_text="Куди лабораторія скидає файли на сервер",
        operator_editable=True,
    ),
    SettingField(
        key="license_key",
        label="Ліцензійний ключ",
        secret=True,
        help_text="Видає власник продукту для цього комп'ютера",
    ),
    # Appended after the original six so existing settings.html template
    # indices (fields[0]..fields[5]) stay stable — these new fields are
    # looked up by key, not position.
    SettingField(
        key="google_auth_mode",
        label="Спосіб авторизації Google",
        help_text="service_account або oauth",
    ),
    SettingField(
        key="google_oauth_client_json",
        label="Google OAuth Client JSON",
        secret=True,
        multiline=True,
        help_text="Вміст JSON «Desktop app» OAuth-клієнта з Google Cloud Console",
    ),
    SettingField(
        key="google_oauth_refresh_token",
        label="Google OAuth Refresh Token",
        secret=True,
        help_text="Заповнюється автоматично після входу через Google — не редагувати вручну",
    ),
]

OPERATOR_EDITABLE_KEYS = {field.key for field in SETTING_FIELDS if field.operator_editable}

# Non-secret preference keys stored in the same AppSetting table but NOT part of
# the credentials screen (SETTING_FIELDS) — set from their own settings screens.
# mail_default_material: which material the mail triage assumes when a milling
# letter carries no material signal at all (empty string / unset = off). See
# app/mail_reader.py and the /settings/recognition screen.
PREFERENCE_KEYS = {"mail_default_material"}

SETTING_KEYS = {field.key for field in SETTING_FIELDS} | PREFERENCE_KEYS


def get_setting(session: Session, key: str) -> Optional[str]:
    row = session.get(AppSetting, key)
    if row is None or row.value_encrypted is None:
        return None
    return decrypt_value(row.value_encrypted)


def get_all_settings(session: Session) -> dict[str, Optional[str]]:
    return {field.key: get_setting(session, field.key) for field in SETTING_FIELDS}


def set_setting(session: Session, key: str, value: str) -> None:
    if key not in SETTING_KEYS:
        raise ValueError(f"unknown setting key: {key}")
    row = session.get(AppSetting, key)
    encrypted = encrypt_value(value)
    if row is None:
        session.add(AppSetting(key=key, value_encrypted=encrypted))
    else:
        row.value_encrypted = encrypted


def get_google_sheet_id(session: Session) -> str:
    return get_setting(session, "google_sheet_id") or GOOGLE_SHEET_ID


def get_google_service_account_json(session: Session) -> Optional[str]:
    """Returns JSON content if set via the settings screen, else None.

    Callers should fall back to reading GOOGLE_SERVICE_ACCOUNT_JSON as a file
    path (the .env-based bootstrap mode) when this returns None.
    """
    return get_setting(session, "google_service_account_json")


def get_export_folder_path(session: Session) -> str:
    return get_setting(session, "export_folder_path") or EXPORT_FOLDER_PATH


def get_technician_files_path(session: Session) -> Optional[str]:
    return get_setting(session, "technician_files_path")


def get_imap_login(session: Session) -> Optional[str]:
    return get_setting(session, "imap_login")


def get_imap_password(session: Session) -> Optional[str]:
    return get_setting(session, "imap_password")


def get_license_key(session: Session) -> Optional[str]:
    return get_setting(session, "license_key")


def get_google_auth_mode(session: Session) -> str:
    """"service_account" (default, JSON key) or "oauth" (personal Google
    account sign-in) — which credentials app/sheets.py should build."""
    return get_setting(session, "google_auth_mode") or "service_account"


def get_google_oauth_client_json(session: Session) -> Optional[str]:
    return get_setting(session, "google_oauth_client_json")


def get_google_oauth_refresh_token(session: Session) -> Optional[str]:
    return get_setting(session, "google_oauth_refresh_token")


def get_mail_default_material(session: Session) -> Optional[str]:
    """Material name the mail triage assumes for a milling letter with no
    material signal (e.g. «Цирконій»), or None/"" when the rule is off."""
    value = get_setting(session, "mail_default_material")
    return value or None


def set_mail_default_material(session: Session, value: str | None) -> None:
    set_setting(session, "mail_default_material", (value or "").strip())
