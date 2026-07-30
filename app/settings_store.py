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
    ),
    SettingField(
        key="technician_files_path",
        label="Шлях до папки робіт техніків",
        help_text="Куди лабораторія скидає файли на сервер",
    ),
]

SETTING_KEYS = {field.key for field in SETTING_FIELDS}


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
