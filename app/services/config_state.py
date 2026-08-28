"""«Чи це взагалі налаштовано» — предикати над збереженими секретами.

Порожній рядок і відсутній ключ мають означати одне й те саме, а для Google
ще й залежати від режиму входу (сервісний акаунт чи OAuth). Ці правила
питають і черга (пара індикаторів синку), і пошта, і налаштування — тож вони
живуть в одному місці, а не переписуються по роутерах.

Тут же — довірені корені файлової системи: єдиний список тек, усередині яких
застосунку взагалі дозволено відкривати щось у Провіднику.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from app.config import MAIL_ATTACHMENTS_PATH
from app.settings_store import (
    get_export_folder_path,
    get_google_auth_mode,
    get_google_oauth_client_json,
    get_google_oauth_refresh_token,
    get_google_service_account_json,
    get_google_sheet_id,
    get_imap_login,
    get_imap_password,
)


def sheets_configured(db: Session) -> bool:
    if not (get_google_sheet_id(db) or "").strip():
        return False
    if get_google_auth_mode(db) == "oauth":
        return bool(
            (get_google_oauth_client_json(db) or "").strip()
            and (get_google_oauth_refresh_token(db) or "").strip()
        )
    return bool((get_google_service_account_json(db) or "").strip())


def imap_configured(db: Session) -> bool:
    return bool((get_imap_login(db) or "").strip() and (get_imap_password(db) or "").strip())


def mail_trusted_roots(db: Session) -> list[Path]:
    roots: list[Path] = []
    mail_root = str(MAIL_ATTACHMENTS_PATH).strip()
    export_root = (get_export_folder_path(db) or "").strip()
    if mail_root:
        roots.append(Path(mail_root))
    if export_root:
        roots.append(Path(export_root))
    return roots


def mail_preview_roots(db: Session) -> dict[str, str | None]:
    return {"mail": str(MAIL_ATTACHMENTS_PATH), "export": get_export_folder_path(db)}
