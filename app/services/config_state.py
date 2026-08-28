"""«Чи це взагалі налаштовано» — предикати над збереженими секретами.

Порожній рядок і відсутній ключ мають означати одне й те саме, а для Google
ще й залежати від режиму входу (сервісний акаунт чи OAuth). Ці правила
питають і черга (пара індикаторів синку), і пошта, і налаштування — тож вони
живуть в одному місці, а не переписуються по роутерах.

Тут же — довірені корені файлової системи: єдиний список тек, усередині яких
застосунку взагалі дозволено відкривати щось у Провіднику.
"""

from pathlib import Path

import gspread

from sqlalchemy.orm import Session

from app.config import MAIL_ATTACHMENTS_PATH
from app.settings_store import (
    get_service_account_email,
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

def sheets_access_error_message(db: Session, exc: BaseException) -> str:
    """Turn a failed spreadsheet open into the one sentence that says what to
    DO about it.

    The two real-world failures look identical in the old catch-all wording
    ("перевірте ID, ключ і доступ"), yet need opposite actions: a 404 means the
    id is wrong, a 403 means the file exists but was never shared with the
    account we authenticate as. Naming the service-account address in the 403
    case matters — it is exactly what has to be pasted into Google's Share
    dialog. Raw Google error text is still never echoed.
    """
    status = None
    if isinstance(exc, gspread.exceptions.APIError):
        status = getattr(getattr(exc, "response", None), "status_code", None)

    if isinstance(exc, gspread.exceptions.SpreadsheetNotFound) or status == 404:
        return "Таблицю з таким ID не знайдено — перевірте ID або вставте посилання на таблицю"

    if status == 403:
        email = get_service_account_email(db)
        if get_google_auth_mode(db) == "oauth":
            return "Акаунт Google не має доступу до цієї таблиці — увійдіть тим акаунтом, що бачить таблицю"
        if email:
            return (
                "Таблиця не відкрита для сервісного акаунта. Відкрийте її в Google → "
                f"«Поділитися» → додайте {email} як Редактора"
            )
        return "Немає доступу до таблиці — надайте сервісному акаунту права Редактора"

    if status in (401, 400):
        return "Google не прийняв облікові дані — перевірте JSON-ключ сервісного акаунта"

    return "Не вдалося відкрити таблицю. Перевірте ID, ключ і доступ до таблиці"


