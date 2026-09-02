"""Налаштування (адмін): секрети, шляхи, оператори, бекапи, оновлення.

Секрети (Google Sheet ID, сервісний JSON, IMAP-пароль, шлях до export) свідомо
НЕ живуть у .env: їх вводять на цьому екрані й тримають у базі зашифрованими
(CLAUDE.md §7). У .env лишається один ключ — той, яким шифрується сама база.

Дії, що керують МАШИНОЮ (відкрити теку, поставити оновлення), додатково
обмежені loopback: мати валідну сесію мало, запит має прийти з цього ж ПК.
"""

import json
import logging
import socket
import ssl
import uuid
from threading import Thread
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from imap_tools import MailBox
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.business_day import set_rollover
from app.__version__ import VERSION
from app.auth import hash_password
from app.backup import (
    BackupFormatError,
    BackupPasswordError,
    create_backup,
    restore_backup,
)
from app.changelog import load_changelog
from app.config import DB_PATH, MAIL_ATTACHMENTS_PATH
from app.db import SessionLocal
from app.google_oauth import OAuthFlowError, parse_client_config, run_authorization_flow
from app.mail_reader import IMAP_HOST, IMAP_TIMEOUT_SECONDS
from app.mail_spool import analyze_spool, prune_spool
from app.material_catalog import (
    MaterialCatalogError,
    add_alias,
    add_material,
    backfill_orders,
    delete_alias,
    ensure_seeded,
    list_materials,
    unresolved_order_count,
)
from app.models import (
    Furnace,
    Machine,
    AppSetting,
    EmailMessage,
    MailFilterCategory,
    MailFilterRule,
    Order,
    User,
)
from app.monthly_backup import list_snapshots
from app.routers.deps import (
    get_current_user,
    get_db,
    is_loopback_request,
    templates,
    toast_response,
)
from app.routers.mail import _mail_filter_categories
from app.services import machines as machines_service
from app.services.config_state import (
    imap_configured,
    sheets_access_error_message,
    sheets_configured,
)
from app.services.shift import open_note_count as open_shift_note_count
from app.services.operators import normalize_initial, validate_initial
from app.settings_store import (
    CLEARABLE_SETTING_KEYS,
    OPERATOR_EDITABLE_KEYS,
    SECRET_SETTING_KEYS,
    SETTING_FIELDS,
    NOTIFY_EVENTS,
    extract_sheet_id,
    get_all_settings,
    get_day_rollover_time,
    get_export_folder_path,
    get_google_oauth_client_json,
    get_furnace_background,
    get_furnace_vnc_password,
    get_machine_vnc_password,
    get_mail_default_material,
    get_setting,
    get_imap_login,
    get_imap_password,
    get_mail_download_all,
    get_notify_events,
    get_notify_position,
    get_notify_style,
    get_service_account_email,
    get_technician_files_path,
    set_furnace_background,
    set_mail_default_material,
    set_mail_download_all,
    set_notify_prefs,
    set_setting,
)
from app.crypto import encrypt_value
from app.services.furnace import FurnaceConfigError, list_furnaces, validate_address
from app.sheet_sync_service import SheetSyncError, summary_message, sync_google_sheets
from app.sheets import measure_sheet_weight, open_spreadsheet, reset_sheets_cache
from app.sync_control import MAIL_SYNC_INTERVAL_SECONDS, SHEET_SYNC_INTERVAL_SECONDS
from app.sync_heartbeat import sync_status_pair
from app.update_check import (
    _update_check_tick,
    download_and_verify,
    get_known_update,
    launch_silent_install,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def settings_changed_at(db: Session, keys: tuple[str, ...]) -> dict[str, str]:
    """`AppSetting.updated_at` per key, formatted "12.08.26".

    Answers "а коли ми міняли пароль пошти?" without touching the value: the
    timestamp column is plaintext, only `value_encrypted` is a secret. Absolute
    dates, not "N days ago" — a settings screen is consulted rarely, so the
    calendar date is what the operator can actually cross-reference.
    """
    rows = db.scalars(select(AppSetting).where(AppSetting.key.in_(keys))).all()
    return {
        row.key: row.updated_at.strftime("%d.%m.%y")
        for row in rows
        if row.updated_at is not None
    }


def check_path_status(raw_path: str) -> dict[str, str]:
    """Live filesystem probe for the Налаштування path fields (export_folder_path /
    technician_files_path — CLAUDE.md section 7 "мобільним під різні ситуації та ПК").

    A saved path is just a string; nothing else in the save flow checks that it
    actually resolves on *this* machine. `os.access(path, os.W_OK)` is not trusted
    alone — it is unreliable for some Windows network shares — so writability is
    proven with a real create+delete of a small marker file, mirroring the
    defensive try/except style already used in app/mail_export.py.
    """
    value = (raw_path or "").strip()
    if not value:
        return {"state": "neutral", "message": ""}

    path = Path(value)
    try:
        exists = path.exists()
    except OSError:
        return {
            "state": "error",
            "message": "Шлях недоступний на цьому комп'ютері — перевірте диск або мережу",
        }

    if not exists:
        return {
            "state": "error",
            "message": "Шлях не знайдено — можливо, диск не підключений на цьому ПК або є одруківка",
        }
    if not path.is_dir():
        return {
            "state": "error",
            "message": "Це не папка — вказано файл замість каталогу",
        }

    marker = path / f".orderdesk-check-{uuid.uuid4().hex}.tmp"
    try:
        marker.write_bytes(b"")
    except OSError:
        return {
            "state": "warning",
            "message": "Папку знайдено, але немає прав на запис — читання може працювати, збереження файлів — ні",
        }
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass

    return {"state": "success", "message": "Папку знайдено, доступна для запису"}


def require_settings_admin(request: Request, db: Session):
    """Admin + loopback gate shared by the settings mutation routes. Returns the
    user; raises the same 401/403s the other settings POSTs use."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    return user


@router.get("/settings", response_class=HTMLResponse)
def get_settings(
    request: Request,
    saved: str | None = None,
    welcome: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    # Full page is reachable by any operator now — only the Шляхи папок card
    # (below, gated per-field via operator_editable) and the two path
    # HTMX checks are actually operator-facing; settings.html hides every
    # other card behind {% if user.role == 'адмін' %}, and the POST handler
    # enforces the same boundary server-side regardless of what the DOM shows.
    # Секрети НЕ їдуть у шаблон значеннями — лише ознакою «задано».
    # get_all_settings повертає їх розшифрованими, тож у контексті лежав
    # відкритий IMAP-пароль, service-account JSON і refresh token. Жоден
    # шаблон їх не друкує, але від витоку захищала тільки дисципліна автора:
    # одне майбутнє {{ values['imap_password'] }} або сторінка помилки Jinja
    # з дампом контексту — і пароль пошти в HTML.
    _raw = get_all_settings(db)
    values = {k: ("" if k in SECRET_SETTING_KEYS else v) for k, v in _raw.items()}
    values_set = {k: bool(_raw.get(k)) for k in SECRET_SETTING_KEYS}
    operators = db.scalars(select(User).order_by(User.created_at)).all() if user.role == "адмін" else []
    settings_flash = request.session.pop("settings_flash", None)

    # Setup-wizard progress (settings.html "Майстер" layout): five steps, a
    # boolean per step for "готово". These flags are read-only derivations of
    # the same get_setting values used everywhere else — nothing here changes
    # how anything is saved or decrypted.
    google_configured = sheets_configured(db)
    imap_ready = imap_configured(db)
    paths_set = bool(
        (get_export_folder_path(db) or "").strip()
        and (get_technician_files_path(db) or "").strip()
    )
    # Independent of the `operators` list above, which is empty for non-admins.
    operators_exist = db.scalar(select(func.count()).select_from(User)) > 0
    backup_available = True  # a snapshot can always be created — no prerequisite
    setup_steps_total = 5
    setup_steps_done = sum(
        1
        for done in (
            google_configured,
            operators_exist,
            backup_available,
            imap_ready,
            paths_set,
        )
        if done
    )

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "fields": SETTING_FIELDS,
            "values": values,
            "values_set": values_set,
            "user": user,
            "saved": saved is not None or (settings_flash and settings_flash["kind"] == "success"),
            "saved_message": (
                settings_flash["message"]
                if settings_flash and settings_flash["kind"] == "success" and settings_flash.get("message")
                else None
            ),
            "welcome": welcome is not None,
            "sheets_configured": sheets_configured(db),
            "imap_configured": imap_configured(db),
            "google_configured": google_configured,
            # The address the spreadsheet must be shared with — without it on
            # screen there is no way to know what to paste into Google's Share
            # dialog, which is the whole of "connecting" in service-account mode.
            "service_account_email": get_service_account_email(db),
            # Curated changelog from CHANGELOG.md, rendered in «Про застосунок».
            "changelog": load_changelog(),
            # Popup-notification preferences («Спливаючі сповіщення»).
            "notify_style": get_notify_style(db),
            "notify_position": get_notify_position(db),
            "notify_events": get_notify_events(db),
            "notify_all": NOTIFY_EVENTS,
            "paths_set": paths_set,
            "operators_exist": operators_exist,
            "backup_available": backup_available,
            "monthly_snapshots": [
                {
                    "name": p.name,
                    "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
                }
                for p in list_snapshots(DB_PATH)
            ],
            "setup_steps_done": setup_steps_done,
            "setup_steps_total": setup_steps_total,
            "operators": operators,
            # Background-loop liveness, same source the queue sidebar renders.
            # "Стан системи" is where an operator actually looks for it, and the
            # stale-heartbeat detector (STALE_HEARTBEAT_MULTIPLIER) is the one
            # signal that distinguishes "quiet because idle" from "worker died".
            "sync_status": sync_status_pair(db, datetime.now()),
            "sync_intervals": {
                "mail": MAIL_SYNC_INTERVAL_SECONDS // 60,
                "sheet": SHEET_SYNC_INTERVAL_SECONDS // 60,
            },
            "changed_at": settings_changed_at(
                db,
                (
                    "google_sheet_id",
                    "google_service_account_json",
                    "google_oauth_client_json",
                    "imap_login",
                    "imap_password",
                    "export_folder_path",
                    "technician_files_path",
                ),
            ),
            # «Фільтри пошти» section — same shared panel as the filtered tab.
            "filter_rules": db.scalars(
                select(MailFilterRule).order_by(MailFilterRule.id.desc())
            ).all(),
            "filter_categories": _mail_filter_categories(db),
            "filter_category_rows": db.scalars(
                select(MailFilterCategory).order_by(MailFilterCategory.id.asc())
            ).all(),
            "mail_download_all": get_mail_download_all(db),
            # Пічки: рядки таблиці як є, паролі — НІКОЛИ. Назад у поле секрет
            # не підставляється, у шаблон іде лише ознака «збережено».
            "furnaces": list_furnaces(db),
            "furnace_password_set": bool(get_furnace_vnc_password(db)),
            "furnace_bg": get_furnace_background(db),
            # Верстати: той самий контракт — рядки без паролів, лише ознака.
            "machines": machines_service.list_machines(db),
            "machine_password_set": bool(get_machine_vnc_password(db)),
            "spool_report": (_spool_report := analyze_spool(db, Path(MAIL_ATTACHMENTS_PATH))),
            # "Стан системи" flow map — honest, cheap counts (one scalar each).
            # No export-folder scan here; that's the heavy walk we keep off page load.
            "state_nodes": [
                {
                    "n": db.scalar(
                        select(func.count())
                        .select_from(EmailMessage)
                        .where(
                            EmailMessage.status == "нове",
                            EmailMessage.filter_category.is_(None),
                        )
                    ) or 0,
                    "l": "Пошта",
                    "u": "у тріажі",
                },
                {"n": _spool_report.total_dirs, "l": "Спул", "u": f"{_spool_report.total_mb} МБ"},
                {
                    "n": db.scalar(
                        select(func.count())
                        .select_from(Order)
                        .where(Order.status != "видано", Order.archived_at.is_(None))
                    ) or 0,
                    "l": "Черга",
                    "u": "активні",
                },
                {
                    "n": db.scalar(
                        select(func.count())
                        .select_from(Order)
                        .where(Order.archived_at.is_not(None))
                    ) or 0,
                    "l": "Архів",
                    "u": "робіт",
                },
            ],
            "error": error or (
                settings_flash["message"]
                if settings_flash and settings_flash["kind"] == "error"
                else None
            ),
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def post_settings(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    is_admin = user.role == "адмін"

    form = await request.form()
    action = form.get("action", "save")
    # Field-level enforcement, not just a hidden card: an operator's rendered
    # form only contains the two path inputs, but a hand-crafted POST could
    # still include google_sheet_id/imap_password — silently drop anything
    # outside OPERATOR_EDITABLE_KEYS for a non-admin rather than trusting the
    # DOM to have hidden it.
    for field in SETTING_FIELDS:
        if not is_admin and field.key not in OPERATOR_EDITABLE_KEYS:
            continue
        value = form.get(field.key, "").strip()
        if field.key == "google_sheet_id":
            # Operators paste the whole address-bar URL; store the bare id.
            value = extract_sheet_id(value)
        # Порожнє = «не міняти» ЛИШЕ для секретів: їхнє поле рендериться
        # порожнім навмисно (placeholder «збережено»), тож повторний сабміт
        # не має їх стерти. Для шляхів і Sheet ID порожнє = «прибрати»,
        # інакше помилковий мережевий шлях, що вішає видачу, неможливо було
        # зняти — а тост при цьому рапортував «Збережено».
        if value or field.key in CLEARABLE_SETTING_KEYS:
            set_setting(db, field.key, value)
    db.commit()

    # Межа робочого дня живе в памʼяті процесу (business_today() кличеться на
    # кожен рядок черги, у БД по неї ходити не можна) — оновлюємо одразу після
    # збереження, інакше нове значення підхопилось би лише після рестарту.
    set_rollover(get_day_rollover_time(db))

    if action == "save_and_sync" and not is_admin:
        action = "save"

    # HTMX save keeps the operator on the section they were editing. The full
    # POST redirected to /settings?saved=1 — no #hash — which under the console
    # layout lands on «Стан системи» instead of the form just saved. Answer 204
    # (nothing to swap; the DOM already shows what was typed) and report the
    # outcome through the app-wide toast channel. Without JS the plain form
    # still posts here and still gets the redirect below.
    hx = request.headers.get("HX-Request") == "true"

    if action == "save_and_sync":
        try:
            summary = sync_google_sheets(db)
        except SheetSyncError as exc:
            if hx:
                return toast_response("Синхронізація: " + str(exc), kind="error")
            request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
            return RedirectResponse("/settings?welcome=1", status_code=303)
        message = summary_message(summary)
        if hx:
            return toast_response(message, kind="success")
        request.session["sync_flash"] = {"kind": "success", "message": message}
        return RedirectResponse("/", status_code=303)

    if hx:
        return toast_response("Збережено", kind="success")
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/check-path", response_class=HTMLResponse)
def check_settings_path(
    request: Request,
    kind: str = Form(""),
    export_folder_path: str | None = Form(None),
    technician_files_path: str | None = Form(None),
    sum3d_projects_path: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Live, per-machine reachability/writability check for A3 (CLAUDE.md
    section 7's "мобільним під різні ситуації та ПК"). HTMX-triggered on
    blur/typing from settings.html — see the two `hx-post`-wired path inputs
    there. Both real field names are accepted and `kind` just selects which
    one this particular request means; the response never touches the DB.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    # Operator-editable (see OPERATOR_EDITABLE_KEYS) — both fields this check
    # serves are filesystem paths, not credentials, so any logged-in user may
    # probe them, same as they may now save them.
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if kind == "sum3d":
        raw_path = sum3d_projects_path
    elif kind == "export":
        raw_path = export_folder_path
    else:
        raw_path = technician_files_path
    result = check_path_status(raw_path or "")

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )


def _imap_error_reason(exc: Exception) -> str:
    """Turn a raw IMAP failure into a specific, operator-actionable Ukrainian
    reason. The lab operator needs to know WHICH problem it is: a rejected
    app-password / disabled IMAP is a completely different fix from "no
    internet". ukr.net's own login-rejection is the common case and its meaning
    ("check the app-password, enable IMAP access") is genuinely useful, so we
    surface that intent rather than a generic "try again". We still don't echo
    the raw server byte-string into the UI — just the classified reason."""
    from imap_tools.errors import MailboxLoginError

    if isinstance(exc, MailboxLoginError):
        return (
            "Пошта ukr.net відхилила вхід. Найімовірніше протермінувався або "
            "змінився пароль для програм, або в скриньці вимкнено IMAP-доступ. "
            "Згенеруйте новий пароль для програм на ukr.net і увімкніть IMAP, "
            "потім вставте пароль тут."
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Пошта imap.ukr.net не відповіла вчасно — перевірте інтернет-з'єднання."
    if isinstance(exc, ssl.SSLError):
        return "Помилка захищеного з'єднання з imap.ukr.net (можливо, заважає проксі/антивірус)."
    if isinstance(exc, (ConnectionError, socket.gaierror, OSError)):
        return "Немає з'єднання з imap.ukr.net — перевірте інтернет або проксі."
    return "Не вдалося підключитися до пошти. Перевірте логін, пароль для програм та інтернет."


def _imap_check_response(
    request: Request,
    result: dict,
    *,
    toast_kind: str,
    toast_message: str | None = None,
) -> HTMLResponse:
    """Render the inline check pill AND attach an HX-Trigger toast so the same
    reason also pops as a notification inside the CRM (see app.js showToast).
    Belt-and-braces: the pill stays next to the field, the toast makes sure the
    operator can't miss a failure even if the field is scrolled out of view."""
    response = templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )
    # The toast payload rides along as an HX-Trigger header so app.js can pop it
    # without a per-route client handler. getattr-guarded so a test that stubs
    # TemplateResponse to return a plain dict isn't forced to fake a headers map.
    headers = getattr(response, "headers", None)
    if headers is not None:
        headers["HX-Trigger"] = json.dumps(
            {"toast": {"message": toast_message or result["message"], "kind": toast_kind}}
        )
    return response


def _probe_imap_login(login: str | None, password: str | None) -> dict:
    """LOGIN-only probe against the given credentials. Returns a check-result
    dict (state + message). Shared by the save route and the manual test button
    so both give the operator the same specific reason."""
    if not login or not password:
        return {"state": "error", "message": "Спочатку задайте логін і пароль пошти"}
    try:
        with MailBox(IMAP_HOST, timeout=IMAP_TIMEOUT_SECONDS).login(login, password):
            pass
    except Exception as exc:  # noqa: BLE001 — classified into a safe reason below
        logger.warning(
            "IMAP login probe failed for login %s: %s", login, type(exc).__name__
        )
        return {"state": "error", "message": _imap_error_reason(exc)}
    return {"state": "success", "message": "З'єднання з поштою успішне"}


@router.post("/settings/imap", response_class=HTMLResponse)
async def save_imap_settings(request: Request, db: Session = Depends(get_db)):
    """HTMX save for the IMAP credentials block. Saves whatever changed, then
    immediately probes LOGIN and returns the result inline — so the operator
    never gets a silent full-page reload that scrolls back to the top, and sees
    the real reason (as a toast) when it fails. A JS-off client still falls back
    to the plain <form action="/settings"> full POST."""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    form = await request.form()
    login = (form.get("imap_login") or "").strip()
    password = (form.get("imap_password") or "").strip()
    # Empty password means "keep the saved one" — the field renders blank on
    # purpose (placeholder "•••• збережено"), so a save that only edits the login
    # must not wipe the stored password.
    if login:
        set_setting(db, "imap_login", login)
    if password:
        set_setting(db, "imap_password", password)
    db.commit()

    result = _probe_imap_login(get_imap_login(db), get_imap_password(db))
    if result["state"] == "success":
        return _imap_check_response(
            request, result, toast_kind="success", toast_message="Пошту збережено й підключено"
        )
    return _imap_check_response(
        request, result, toast_kind="error", toast_message="Пошта: " + result["message"]
    )


@router.post("/settings/test-imap", response_class=HTMLResponse)
def test_imap_connection(request: Request, db: Session = Depends(get_db)):
    """A4: real IMAP LOGIN-only probe against whatever is CURRENTLY SAVED in
    the DB (not unsaved form values — save first, same as "Зберегти й
    синхронізувати" already works for Google Sheets). Never fetches messages;
    the raw server error is classified into a specific, safe reason
    (_imap_error_reason) rather than echoed verbatim.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    result = _probe_imap_login(get_imap_login(db), get_imap_password(db))
    toast_kind = "success" if result["state"] == "success" else "error"
    toast_message = (
        result["message"]
        if result["state"] == "success"
        else "Пошта: " + result["message"]
    )
    return _imap_check_response(
        request, result, toast_kind=toast_kind, toast_message=toast_message
    )


@router.post("/settings/test-sheets", response_class=HTMLResponse)
def test_sheets_connection(request: Request, db: Session = Depends(get_db)):
    """Read-only Google Sheets access probe for the settings "Майстер" — opens
    the spreadsheet with whatever sheet id / service-account JSON is CURRENTLY
    SAVED (same "save first" contract as test-imap) and confirms the service
    account can actually reach it, without importing any rows. A successful
    "Зберегти й синхронізувати" already proves the same, but this lets the
    admin verify access without mutating the queue. Raw gspread/Google error
    text is never surfaced to the UI, matching the test-imap discipline.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if not sheets_configured(db):
        result = {
            "state": "error",
            "message": "Спочатку збережіть ID таблиці та JSON-ключ сервісного акаунта",
        }
    else:
        try:
            spreadsheet = open_spreadsheet(db=db)
            # Touch the worksheet list so a permissions/id error surfaces here,
            # not just an object we never actually read from.
            tabs = spreadsheet.worksheets()
        except Exception as exc:
            logger.warning("Google Sheets access test failed")
            result = {"state": "error", "message": sheets_access_error_message(db, exc)}
        else:
            result = {
                "state": "success",
                "message": f"Доступ підтверджено · {len(tabs)} вкладок",
            }

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )


@router.post("/settings/google-oauth/start", response_class=HTMLResponse)
def start_google_oauth(request: Request, db: Session = Depends(get_db)):
    """Runs the "Sign in with Google" flow using the CURRENTLY SAVED OAuth
    client JSON (same "save first" contract as test-sheets) — opens the
    admin's system browser on this PC, waits for the consent redirect, and
    stores the resulting refresh token encrypted. On success also switches
    google_auth_mode to "oauth" so subsequent Sheets calls use it."""
    require_settings_admin(request, db)

    client_json = (get_google_oauth_client_json(db) or "").strip()
    if not client_json:
        result = {
            "state": "error",
            "message": "Спочатку вставте й збережіть OAuth Client JSON",
        }
    else:
        try:
            config = parse_client_config(client_json)
            refresh_token = run_authorization_flow(config)
        except OAuthFlowError as exc:
            logger.warning("Google OAuth sign-in failed: %s", exc)
            result = {"state": "error", "message": str(exc)}
        except Exception:
            logger.exception("Google OAuth sign-in failed unexpectedly")
            result = {
                "state": "error",
                "message": "Не вдалося завершити вхід через Google. Спробуйте ще раз",
            }
        else:
            set_setting(db, "google_oauth_refresh_token", refresh_token)
            set_setting(db, "google_auth_mode", "oauth")
            db.commit()
            reset_sheets_cache()
            result = {"state": "success", "message": "Вхід через Google виконано"}

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )


@router.post("/settings/google-oauth/disconnect", response_class=HTMLResponse)
def disconnect_google_oauth(request: Request, db: Session = Depends(get_db)):
    """Clears the stored refresh token and switches back to the service-account
    mode — lets an admin re-run the sign-in flow (e.g. with a different Google
    account) without leaving a stale token behind."""
    require_settings_admin(request, db)
    set_setting(db, "google_oauth_refresh_token", "")
    set_setting(db, "google_auth_mode", "service_account")
    db.commit()
    reset_sheets_cache()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/settings/materials", response_class=HTMLResponse)
def get_materials_settings(request: Request, db: Session = Depends(get_db)):
    """Screen: material library management (admin). Lists categories with their
    alias rules, plus the count of orders whose colour is still unclassified."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    ensure_seeded(db)
    flash = request.session.pop("materials_flash", None)
    return templates.TemplateResponse(
        request,
        "settings_materials.html",
        {
            "page_title": "Бібліотека матеріалів",
            "user": user,
            "materials": list_materials(db),
            "unresolved_count": unresolved_order_count(db),
            "flash": flash,
        },
    )


@router.post("/settings/materials/alias/add")
def add_material_alias(
    request: Request,
    material_id: int = Form(...),
    pattern: str = Form(...),
    match_type: str = Form("contains"),
    db: Session = Depends(get_db),
):
    require_settings_admin(request, db)
    try:
        add_alias(db, material_id, pattern, match_type)
        # Apply the new rule to every order, including already-classified ones.
        changed = backfill_orders(db, only_unresolved=False)
        db.commit()
        request.session["materials_flash"] = {
            "kind": "success",
            "message": f"Правило додано. Перекласифіковано робіт: {changed}.",
        }
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/alias/{alias_id}/delete")
def remove_material_alias(alias_id: int, request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    delete_alias(db, alias_id)
    # Re-resolve from scratch so orders that only matched the deleted rule are
    # re-evaluated against the remaining rules (may become unresolved again).
    for order in db.scalars(select(Order)).all():
        order.material_id = None
    backfill_orders(db, only_unresolved=False)
    db.commit()
    request.session["materials_flash"] = {"kind": "success", "message": "Правило видалено."}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/add")
def create_material(
    request: Request,
    name: str = Form(...),
    is_production: str = Form("on"),
    db: Session = Depends(get_db),
):
    require_settings_admin(request, db)
    try:
        add_material(db, name, is_production=is_production == "on")
        db.commit()
        request.session["materials_flash"] = {"kind": "success", "message": "Матеріал додано."}
    except MaterialCatalogError as exc:
        db.rollback()
        request.session["materials_flash"] = {"kind": "error", "message": str(exc)}
    return RedirectResponse("/settings/materials", status_code=303)


@router.post("/settings/materials/reclassify")
def reclassify_materials(request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    for order in db.scalars(select(Order)).all():
        order.material_id = None
    changed = backfill_orders(db, only_unresolved=False)
    db.commit()
    request.session["materials_flash"] = {
        "kind": "success",
        "message": f"Перекласифіковано робіт: {changed}.",
    }
    return RedirectResponse("/settings/materials", status_code=303)


@router.get("/settings/recognition", response_class=HTMLResponse)
def get_recognition_settings(request: Request, db: Session = Depends(get_db)):
    """Screen: mail recognition settings (admin). Holds the default-material
    fallback the triage applies to a signal-less milling letter, and points to
    the two editable dictionaries that live elsewhere — the material library
    (synonyms like «врім'янка» → ПММА) and the mail filters (exception
    categories like 3D-друк / моделювання that route a letter out of the queue)."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    flash = request.session.pop("recognition_flash", None)
    return templates.TemplateResponse(
        request,
        "settings_recognition.html",
        {
            "page_title": "Розпізнавання пошти",
            "user": user,
            "materials": list_materials(db),
            "default_material": get_mail_default_material(db),
            "flash": flash,
        },
    )


@router.post("/settings/mail-spool/prune")
def prune_mail_spool(request: Request, db: Session = Depends(get_db)):
    """Delete the mail-spool folders analyze_spool considers safe (empty ones,
    orphans with no letter row, and rejected letters past the retention
    window). Operator-triggered only — never a background job, see
    app/mail_spool.py."""
    require_settings_admin(request, db)
    removed, freed = prune_spool(db, Path(MAIL_ATTACHMENTS_PATH))
    mb = round(freed / (1024 * 1024), 1)
    request.session["settings_flash"] = {
        "kind": "success",
        "message": (
            f"Прибрано папок: {removed}, звільнено {mb} МБ."
            if removed
            else "Нічого прибирати — спул чистий."
        ),
    }
    return RedirectResponse("/settings#mail-download", status_code=303)


@router.post("/settings/furnaces")
def add_furnace(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Додати пічку в перелік.

    Адреса перевіряється ДО збереження: криво написаний рядок краще відбити
    тут, ніж потім показувати оператору порожню плитку «немає зв'язку».
    """
    require_settings_admin(request, db)
    try:
        clean_host, clean_port = validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#furnaces", status_code=303)

    if db.scalar(
        select(Furnace).where(Furnace.host == clean_host, Furnace.port == clean_port)
    ):
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Пічка {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#furnaces", status_code=303)

    last = db.scalar(select(func.max(Furnace.sort_order)))
    db.add(
        Furnace(
            name=name.strip() or clean_host,
            host=clean_host,
            port=clean_port,
            enabled=True,
            password_encrypted=encrypt_value(password.strip()) if password.strip() else None,
            sort_order=(last or 0) + 1,
            created_at=datetime.now(),
        )
    )
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Пічку «{name.strip() or clean_host}» додано.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


# Літеральний шлях мусить стояти ПЕРЕД параметризованим: FastAPI приміряє
# маршрути в порядку оголошення, і /settings/furnaces/{furnace_id} нижче радо
# з'їдав «password» як номер пічки (спіймано живою перевіркою — 422 замість
# збереження пароля).
@router.post("/settings/furnaces/background")
def toggle_furnace_background(
    request: Request, enabled: str = Form(""), db: Session = Depends(get_db)
):
    """Увімкнути або вимкнути фотографію-фон на екрані «Пічки».

    Оголошено ВИЩЕ /settings/furnaces/{furnace_id} з тієї ж причини, що й
    /password: FastAPI приміряє маршрути в порядку оголошення й з'їв би слово
    «background» як номер пічки.
    """
    require_settings_admin(request, db)
    set_furnace_background(db, enabled == "1")
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": "Фон екрана «Пічки» увімкнено." if enabled == "1" else "Фон екрана «Пічки» вимкнено.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


@router.post("/settings/furnaces/password")
def save_furnace_password(
    request: Request, password: str = Form(""), db: Session = Depends(get_db)
):
    """Спільний пароль VNC. Порожнє поле означає «не міняти» — з тієї ж
    причини, що й у рядку пічки вище."""
    require_settings_admin(request, db)
    if password.strip():
        set_setting(db, "furnace_vnc_password", password.strip())
        db.commit()
        message = "Спільний пароль пічок збережено."
    else:
        message = "Пароль не змінено — поле лишилось порожнім."
    request.session["settings_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings#furnaces", status_code=303)


@router.post("/settings/furnaces/{furnace_id}")
def update_furnace(
    request: Request,
    furnace_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    enabled: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    """Змінити пічку.

    Порожній пароль означає «не міняти», а не «стерти»: рядок відкривають, щоб
    виправити адресу, і збережений пароль не має зникати від того, що поле не
    заповнили вдруге. Стерти власний пароль можна словом `-` — так є явний
    спосіб повернути пічку на спільний пароль.
    """
    require_settings_admin(request, db)
    furnace = db.get(Furnace, furnace_id)
    if furnace is None:
        raise HTTPException(status_code=404, detail="пічку не знайдено")

    try:
        clean_host, clean_port = validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#furnaces", status_code=303)

    clash = db.scalar(
        select(Furnace).where(
            Furnace.host == clean_host, Furnace.port == clean_port, Furnace.id != furnace_id
        )
    )
    if clash is not None:
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Пічка {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#furnaces", status_code=303)

    furnace.name = name.strip() or clean_host
    furnace.host = clean_host
    furnace.port = clean_port
    furnace.enabled = enabled == "1"
    if password.strip() == "-":
        furnace.password_encrypted = None
    elif password.strip():
        furnace.password_encrypted = encrypt_value(password.strip())
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Пічку «{furnace.name}» збережено.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


@router.post("/settings/furnaces/{furnace_id}/delete")
def delete_furnace(request: Request, furnace_id: int, db: Session = Depends(get_db)):
    """Прибрати пічку з переліку. Її показання лишаються в історії — рядки
    підписані адресою, і чистити їх разом із записом означало б втратити те,
    що вже сталося."""
    require_settings_admin(request, db)
    furnace = db.get(Furnace, furnace_id)
    if furnace is None:
        raise HTTPException(status_code=404, detail="пічку не знайдено")
    name = furnace.name
    db.delete(furnace)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Пічку «{name}» прибрано з переліку.",
    }
    return RedirectResponse("/settings#furnaces", status_code=303)


# ── Верстати ────────────────────────────────────────────────────────────────
# Дзеркало роутів пічок вище, включно з ПАСТКОЮ ПОРЯДКУ: літеральний
# /settings/machines/password мусить стояти ПЕРЕД /settings/machines/{id},
# інакше FastAPI з'їдає слово «password» як номер верстата (спіймано живою
# перевіркою на пічках — 422 замість збереження).


@router.post("/settings/machines")
def add_machine(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    password: str = Form(""),
    agent_token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Додати верстат. Адреса перевіряється ДО збереження — криву краще
    відбити тут, ніж показувати порожню плитку «немає зв'язку»."""
    require_settings_admin(request, db)
    # HTTP-агент типово на 8765; VNC — на 5900. Якщо порт не вказано, беремо
    # за замовчуванням той, що відповідає обраному способу.
    if not port.strip():
        port = "8765" if agent_token.strip() else ""
    try:
        clean_host, clean_port = machines_service.validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#machines", status_code=303)

    if db.scalar(
        select(Machine).where(Machine.host == clean_host, Machine.port == clean_port)
    ):
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Верстат {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#machines", status_code=303)

    last = db.scalar(select(func.max(Machine.sort_order)))
    db.add(
        Machine(
            name=name.strip() or clean_host,
            host=clean_host,
            port=clean_port,
            enabled=True,
            password_encrypted=encrypt_value(password.strip()) if password.strip() else None,
            agent_token_encrypted=encrypt_value(agent_token.strip()) if agent_token.strip() else None,
            sort_order=(last or 0) + 1,
            created_at=datetime.now(),
        )
    )
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Верстат «{name.strip() or clean_host}» додано.",
    }
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/password")
def save_machine_password(
    request: Request, password: str = Form(""), db: Session = Depends(get_db)
):
    """Спільний view-only пароль UltraVNC верстатів. Порожнє = не міняти."""
    require_settings_admin(request, db)
    if password.strip():
        set_setting(db, "machine_vnc_password", password.strip())
        db.commit()
        message = "Спільний пароль верстатів збережено."
    else:
        message = "Пароль не змінено — поле лишилось порожнім."
    request.session["settings_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/{machine_id}")
def update_machine(
    request: Request,
    machine_id: int,
    name: str = Form(...),
    host: str = Form(...),
    port: str = Form(""),
    enabled: str = Form(""),
    password: str = Form(""),
    agent_token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Змінити верстат. Порожній пароль/токен = не міняти; `-` = стерти
    (той самий контракт, що в пічки)."""
    require_settings_admin(request, db)
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail="верстат не знайдено")

    try:
        clean_host, clean_port = machines_service.validate_address(host, port)
    except FurnaceConfigError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings#machines", status_code=303)

    clash = db.scalar(
        select(Machine).where(
            Machine.host == clean_host, Machine.port == clean_port, Machine.id != machine_id
        )
    )
    if clash is not None:
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Верстат {clean_host}:{clean_port} уже в переліку.",
        }
        return RedirectResponse("/settings#machines", status_code=303)

    machine.name = name.strip() or clean_host
    machine.host = clean_host
    machine.port = clean_port
    machine.enabled = enabled == "1"
    if password.strip() == "-":
        machine.password_encrypted = None
    elif password.strip():
        machine.password_encrypted = encrypt_value(password.strip())
    if agent_token.strip() == "-":
        machine.agent_token_encrypted = None
    elif agent_token.strip():
        machine.agent_token_encrypted = encrypt_value(agent_token.strip())
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Верстат «{machine.name}» збережено.",
    }
    return RedirectResponse("/settings#machines", status_code=303)


@router.post("/settings/machines/{machine_id}/delete")
def delete_machine(request: Request, machine_id: int, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    machine = db.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status_code=404, detail="верстат не знайдено")
    name = machine.name
    db.delete(machine)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Верстат «{name}» прибрано з переліку.",
    }
    return RedirectResponse("/settings#machines", status_code=303)


# One self-check probe may not wedge the run. Mirrors the reasoning behind
# mail_sync_service.MAIL_SYNC_DEADLINE_SECONDS: a half-open TLS socket can hang
# an IMAP/Sheets call indefinitely, and here that would stall a threadpool
# worker with the UI showing a spinner forever. Past the deadline the probe is
# abandoned (its thread is left to die on its own) and reported as a failure.
SELFCHECK_STEP_DEADLINE_SECONDS = 20


@router.get("/api/notify-state")
def api_notify_state(request: Request, db: Session = Depends(get_db)):
    """Cheap snapshot the client polls to detect system events worth a popup.

    Deliberately NOT a push channel: the browser compares this against its own
    previous snapshot and raises a toast on a TRANSITION (ok → error, count
    grew). That keeps the trigger logic in one place client-side and means a
    missed poll can never replay an old alert — the next poll just reflects
    reality. Everything here is already computed for the queue page, so this
    costs two scalar counts and two in-memory heartbeat reads.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    status = sync_status_pair(db, datetime.now())
    release = get_known_update()
    return {
        "sheet": status["sheet"]["state"],
        "mail": status["mail"]["state"],
        "sheet_label": status["sheet"]["label"],
        "mail_label": status["mail"]["label"],
        "orders": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status != "видано", Order.archived_at.is_(None))
        ) or 0,
        "mail_pending": db.scalar(
            select(func.count())
            .select_from(EmailMessage)
            .where(EmailMessage.status == "нове", EmailMessage.filter_category.is_(None))
        ) or 0,
        # Works a technician corrected in the sheet and nobody has acknowledged
        # yet. A rise means a fresh correction — the client toasts on that, so
        # the operator learns about it even while looking at the machines.
        "changed": db.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.sheet_changed_at.is_not(None), Order.archived_at.is_(None))
        ) or 0,
        # Відкриті записки передачі зміни — приріст означає, що колега
        # щойно щось передав. Той самий предикат, що й дошка/бейдж
        # (app/services/shift.py), щоб три місця не розходились.
        "shift": open_shift_note_count(db),
        "update": release.version if release else None,
    }


@router.post("/settings/notifications")
async def save_notification_prefs(request: Request, db: Session = Depends(get_db)):
    """Save popup look, placement and which system triggers may fire one.

    Operator-facing (not admin-only): these are per-installation display
    preferences on a single-workstation app, not a security boundary — the same
    reasoning that makes the folder paths operator-editable.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")

    form = await request.form()
    set_notify_prefs(
        db,
        style=(form.get("notify_style") or "").strip(),
        position=(form.get("notify_position") or "").strip(),
        events=form.getlist("notify_events"),
    )
    db.commit()
    if request.headers.get("HX-Request") == "true":
        return toast_response("Налаштування сповіщень збережено")
    return RedirectResponse("/settings#notifications", status_code=303)


@router.post("/settings/sheet-weight", response_class=HTMLResponse)
def settings_sheet_weight(request: Request, db: Session = Depends(get_db)):
    """Weigh the spreadsheet's conditional formatting — read-only diagnostic.

    Answers "чому додавання таке повільне" with numbers instead of guesses: a
    document whose day-tabs are copies of yesterday's accumulates per-cell
    conditional-format rules, and every values call then pays for the whole
    document's metadata. Nothing is modified here; cleaning is a separate,
    explicitly requested action.
    """
    require_settings_admin(request, db)

    if not sheets_configured(db):
        result = {"state": "error", "message": "Спочатку підключіть Google Таблицю"}
        return templates.TemplateResponse(
            request, "_settings_check_result.html", {"result": result}
        )

    try:
        weight = measure_sheet_weight(open_spreadsheet(db=db))
    except Exception as exc:  # noqa: BLE001 — surface a safe reason, never raw Google text
        logger.warning("Sheet weight probe failed", exc_info=True)
        return templates.TemplateResponse(
            request,
            "_settings_check_result.html",
            {"result": {"state": "error", "message": sheets_access_error_message(db, exc)}},
        )

    # Thresholds from the measured test-sheet case: a healthy tab carries a
    # handful of rules; thousands per tab is the copy-a-tab disease.
    avg = weight["avg_rules"]
    if avg >= 200:
        state, verdict = "error", "Таблиця сильно роздута умовним форматуванням — це і є причина повільності"
    elif avg >= 50:
        state, verdict = "warning", "Умовного форматування помітно більше норми"
    else:
        state, verdict = "success", "Умовне форматування в нормі — повільність не через нього"

    message = (
        f"{verdict}. Правил: {weight['total_rules']} у {weight['tab_count']} вкладках "
        f"(в середньому {avg} на вкладку, норма 5–20). "
        f"Дрібних діапазонів 1×1/2×1: {weight['tiny_ranges']}. "
        f"Метадані: {weight['payload_mb']} МБ, читались {weight['fetch_seconds']} с."
    )
    logger.info("SHEET-WEIGHT %s", message)
    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": {"state": state, "message": message}}
    )


@router.post("/settings/selfcheck")
def settings_selfcheck(request: Request, db: Session = Depends(get_db)):
    """"Стан системи" self-check — streams NDJSON, one line per probe, as each
    one finishes: {key, name, ok, warn, detail, ms}, then a final
    {done, passed, total, version}.

    Streaming rather than one batched JSON so the UI's progression is real: the
    row lights up when its probe actually starts and settles when it actually
    returns. Reuses the exact probes behind the individual «Перевірити» buttons,
    so green here means green there. Nothing is mutated and no secret ever
    leaves — only задано / не задано and the same classified messages those
    buttons show. Admin + loopback only, like the other settings mutations.

    Every config value is read from the DB up front: the generator body runs
    after the request's session would otherwise be torn down, so it must not
    touch `db`.
    """
    import json as _json
    import shutil
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTimeout

    require_settings_admin(request, db)

    sheets_ready = sheets_configured(db)
    imap_ready = imap_configured(db)
    imap_login = get_imap_login(db)
    imap_password = get_imap_password(db)
    export_path = get_export_folder_path(db)
    technician_path = get_technician_files_path(db)

    def _sheets():
        if not sheets_ready:
            return False, False, "не налаштовано — ID або JSON-ключ порожні"
        # open_spreadsheet() without a session falls back to the *env* sheet id,
        # not the one saved through this screen — so it needs a real session.
        # The request's own is gone by the time the generator body runs, hence a
        # short-lived session this probe owns and closes (same shape as
        # run_sync_owned_session, minus the watchdog-zombie case).
        probe_db = SessionLocal()
        try:
            spreadsheet = open_spreadsheet(db=probe_db)
            n = len(spreadsheet.worksheets())
        finally:
            probe_db.close()
        return True, False, f"доступ підтверджено · {n} вкладок"

    def _imap():
        if not imap_ready:
            return False, False, "не налаштовано — логін або пароль порожні"
        res = _probe_imap_login(imap_login, imap_password)
        return res["state"] == "success", False, res["message"]

    def _folder(path_str):
        p = (path_str or "").strip()
        if not p:
            return False, False, "шлях не задано"
        pp = Path(p)
        if not pp.exists():
            return False, False, "папку не знайдено за вказаним шляхом"
        if not pp.is_dir():
            return False, False, "шлях вказує не на папку"
        return True, False, "існує й доступна"

    def _disk():
        usage = shutil.disk_usage(Path(DB_PATH).parent)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 2:
            return False, False, f"вільно лише {free_gb:.1f} ГБ (потрібно ≥2 ГБ)"
        return True, free_gb < 10, f"вільно {free_gb:.1f} ГБ"

    def _backup():
        snaps = list_snapshots(DB_PATH)
        if not snaps:
            return True, True, "жодної автоматичної копії ще немає"
        newest = max(s.stat().st_mtime for s in snaps)
        age_days = (_time.time() - newest) / 86400
        return True, age_days > 40, f"остання копія {age_days:.0f} дн. тому"

    def _update():
        rel = get_known_update()
        if rel:
            return True, True, f"доступне оновлення v{rel.version}"
        return True, False, "встановлена версія найновіша"

    steps = [
        ("sheets", "Доступ до Google Таблиці", _sheets),
        ("imap", "IMAP-зʼєднання зі скринькою", _imap),
        ("export", "Папка export доступна на запис", lambda: _folder(export_path)),
        ("technician", "Папка робіт техніків", lambda: _folder(technician_path)),
        ("disk", "Місце на диску (потрібно ≥2 ГБ)", _disk),
        ("backup", "Резервна копія свіжа", _backup),
        ("update", "Наявність оновлення", _update),
    ]

    def _stream():
        passed = 0
        # Manifest first: the UI renders every row (dimmed, named) up front, so
        # the operator sees what is about to be checked instead of rows
        # appearing anonymously one at a time.
        yield _json.dumps(
            {"steps": [{"key": k, "name": n} for k, n, _ in steps]}, ensure_ascii=False
        ) + "\n"
        # daemon threads: an abandoned probe must never hold up shutdown
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selfcheck")
        try:
            for key, name, fn in steps:
                t0 = _time.perf_counter()
                try:
                    ok, warn, detail = pool.submit(fn).result(
                        timeout=SELFCHECK_STEP_DEADLINE_SECONDS
                    )
                except _FTimeout:
                    logger.warning("selfcheck step %s exceeded deadline", key)
                    ok, warn, detail = False, False, (
                        f"немає відповіді понад {SELFCHECK_STEP_DEADLINE_SECONDS} с — перевірку скасовано"
                    )
                    # The wedged worker owns this pool's only thread; give the
                    # remaining steps a fresh one instead of queueing behind it.
                    pool.shutdown(wait=False)
                    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="selfcheck")
                except Exception:
                    logger.warning("selfcheck step %s failed", key, exc_info=True)
                    ok, warn, detail = False, False, "перевірка не виконалась"
                ms = int((_time.perf_counter() - t0) * 1000)
                if ok:
                    passed += 1
                yield _json.dumps(
                    {"key": key, "name": name, "ok": ok, "warn": warn, "detail": detail, "ms": ms},
                    ensure_ascii=False,
                ) + "\n"
            yield _json.dumps(
                {"done": True, "passed": passed, "total": len(steps), "version": VERSION},
                ensure_ascii=False,
            ) + "\n"
        finally:
            pool.shutdown(wait=False)

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        # Chunks must reach the browser as they are produced, not buffered into
        # one response — otherwise the streaming is pointless.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/settings/mail-download/toggle")
def toggle_mail_download_all(
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Flip the "auto-download every incoming letter's attachments" toggle
    (admin). Off (default) keeps the selective whitelist behaviour; on pulls
    files from every sender into the spool as mail arrives. `return_to=mail`
    lands back on the triage screen (the toggle is mirrored in its header),
    otherwise on the settings section."""
    require_settings_admin(request, db)
    new_value = not get_mail_download_all(db)
    set_mail_download_all(db, new_value)
    db.commit()
    message = (
        "Авто-скачування всіх вкладень увімкнено."
        if new_value
        else "Авто-скачування вимкнено — качаються лише довірені відправники."
    )
    if return_to == "mail":
        request.session["toast_flash"] = {"kind": "success", "message": message}
        return RedirectResponse("/mail", status_code=303)
    request.session["settings_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings#mail-download", status_code=303)


@router.post("/settings/recognition/default-material")
def set_recognition_default_material(
    request: Request,
    material_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Set (or clear, with an empty value) the material the triage assumes for a
    milling letter with no material signal. Validated against real catalog names
    so a typo can't silently disable the rule."""
    require_settings_admin(request, db)
    clean = (material_name or "").strip()
    valid_names = {m.name for m in list_materials(db)}
    if clean and clean not in valid_names:
        request.session["recognition_flash"] = {"kind": "error", "message": "Невідомий матеріал."}
        return RedirectResponse("/settings/recognition", status_code=303)
    set_mail_default_material(db, clean)
    db.commit()
    if clean:
        message = f"Дефолт без сигналу: {clean}."
    else:
        message = "Дефолтний матеріал вимкнено."
    request.session["recognition_flash"] = {"kind": "success", "message": message}
    return RedirectResponse("/settings/recognition", status_code=303)


@router.post("/settings/backup/export")
def export_backup(
    request: Request,
    backup_password: str = Form(...),
    backup_password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    """CLAUDE.md section 14: "backup для перенесення ПК" — a full, portable
    snapshot (every table, every secret) re-encrypted under a password the
    admin sets here and now, independent of this machine's DPAPI key. See
    app/backup.py for why that independence is the whole point.
    """
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if len(backup_password) < 8:
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Пароль резервної копії має містити щонайменше 8 символів",
        }
        return RedirectResponse("/settings", status_code=303)
    if backup_password != backup_password_confirm:
        request.session["settings_flash"] = {"kind": "error", "message": "Паролі не збігаються"}
        return RedirectResponse("/settings", status_code=303)

    content = create_backup(db, backup_password)
    filename = f"orderdesk-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/settings/backup/import", response_class=HTMLResponse)
async def import_backup(
    request: Request,
    backup_password: str = Form(...),
    confirm_replace: str = Form(""),
    backup_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Restore is destructive by design (app/backup.py) — every current
    order, client, user, and secret gets replaced with what's in the file,
    not merged. `confirm_replace` is a required checkbox in settings.html so
    that's a deliberate, informed click, not a misplaced file picker.
    """
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if confirm_replace != "on":
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Підтвердьте, що поточні дані буде замінено",
        }
        return RedirectResponse("/settings", status_code=303)

    raw = await backup_file.read()
    try:
        counts = restore_backup(db, raw, backup_password)
    except BackupPasswordError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings", status_code=303)
    except BackupFormatError as exc:
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings", status_code=303)

    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Відновлено: {counts.get('orders', 0)} робіт, {counts.get('clients', 0)} клієнтів, "
        f"{counts.get('users', 0)} операторів. Увійдіть повторно, якщо змінилися облікові дані.",
    }
    return RedirectResponse("/settings", status_code=303)


def _install_update_in_background(release) -> None:
    """Runs on its own daemon thread — download+verify+silent-install can
    take a while (network download, then Inno Setup itself), and the HTTP
    response to the admin's click must not block on any of that."""
    try:
        installer_path = download_and_verify(release)
        launch_silent_install(installer_path)
    except Exception:
        logger.exception("Background update install failed for release %s", release.version)


@router.post("/settings/update/check", response_class=HTMLResponse)
def check_update(request: Request, db: Session = Depends(get_db)):
    """Admin-triggered manual "is there a newer build?" probe for the settings
    "Про застосунок" section. Runs the same one-shot check the daily background
    worker does (_update_check_tick, which refreshes the module-level "last
    known release" that the rail banner also reads), then renders the result as
    an HTMX fragment: an install button when a newer version is found, or a
    reassuring "you're on the latest" otherwise. Network failures never surface
    raw errors — fetch_latest_release swallows them and returns None, same as
    the background path.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="увійдіть в систему")
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    _update_check_tick()
    return templates.TemplateResponse(
        request,
        "_update_check_result.html",
        {"release": get_known_update(), "current_version": VERSION},
    )


@router.post("/settings/update/install")
def install_update(request: Request, db: Session = Depends(get_db)):
    """Admin-triggered install of the update already found by the
    background checker (app/update_check.py). Downloads, verifies the
    checksum, and launches the silent installer + relaunch watchdog on a
    background thread — see _install_update_in_background above and
    launch_silent_install's docstring for the skipifsilent workaround.
    """
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    release = get_known_update()
    if release is None:
        request.session["settings_flash"] = {"kind": "error", "message": "Оновлень немає"}
        return RedirectResponse("/settings", status_code=303)

    try:
        Thread(
            target=_install_update_in_background,
            args=(release,),
            name="order-desk-update-install",
            daemon=True,
        ).start()
    except Exception:
        logger.exception("Failed to start update install thread for release %s", release.version)
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Не вдалося запустити встановлення оновлення",
        }
        return RedirectResponse("/settings", status_code=303)

    request.session["settings_flash"] = {
        "kind": "success",
        "message": "Оновлення встановлюється, застосунок автоматично перезапуститься за кілька секунд",
    }
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/users", response_class=HTMLResponse)
async def create_operator(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()
    full_name = form.get("full_name", "").strip() or None
    role = form.get("role", "оператор").strip() or "оператор"
    initial = normalize_initial(form.get("sheet_initial", ""))

    if not username or not password:
        return RedirectResponse("/settings?error=логін+і+пароль+обов'язкові", status_code=303)

    existing = db.scalar(select(User).where(User.username == username))
    if existing is not None:
        return RedirectResponse("/settings?error=такий+логін+вже+існує", status_code=303)

    if initial is not None:
        err = validate_initial(db, initial, exclude_user_id=None)
        if err:
            return RedirectResponse(f"/settings?error={quote(err)}", status_code=303)

    db.add(
        User(
            username=username,
            password_hash=hash_password(password),
            full_name=full_name,
            role=role,
            sheet_initial=initial,
        )
    )
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/users/{user_id}/initial", response_class=HTMLResponse)
async def set_operator_initial(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Assign/change/clear an operator's sheet letter (admin only). Empty clears
    it (that operator's Sum3D writes then leave "Прорахував" untouched)."""
    admin = get_current_user(request, db)
    if admin is None:
        return RedirectResponse("/login", status_code=303)
    if admin.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")

    form = await request.form()
    initial = normalize_initial(form.get("sheet_initial", ""))
    if initial is not None:
        err = validate_initial(db, initial, exclude_user_id=user_id)
        if err:
            return RedirectResponse(f"/settings?error={quote(err)}", status_code=303)

    target.sheet_initial = initial
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/users/{user_id}/toggle-active", response_class=HTMLResponse)
async def toggle_operator_active(
    request: Request, user_id: int, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")
    if target.id == user.id:
        return RedirectResponse(
            "/settings?error=не+можна+деактивувати+власний+обліковий+запис", status_code=303
        )

    target.is_active = not target.is_active
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/users/{user_id}/reset-password", response_class=HTMLResponse)
async def reset_operator_password(
    request: Request, user_id: int, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="користувача не знайдено")

    form = await request.form()
    new_password = form.get("new_password", "").strip()
    if not new_password:
        return RedirectResponse("/settings?error=введіть+новий+пароль", status_code=303)

    target.password_hash = hash_password(new_password)
    db.commit()

    return RedirectResponse("/settings?saved=1", status_code=303)


# ── Telegram-пуш форми зворотного зв'язку ──────────────────────────────────
# Бот уже є (VARTAAIR) — сюди вводять лише його токен і прив'язують chat_id.
# Секрет (токен) зберігається зашифровано, як решта секретів (CLAUDE.md §7);
# порожнє поле токена означає «не міняти» — так само, як для пароля пошти.

@router.get("/settings/feedback", response_class=HTMLResponse)
def get_feedback_settings(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != "адмін":
        raise HTTPException(status_code=403, detail="лише для адміністратора")

    flash = request.session.pop("feedback_settings_flash", None)
    return templates.TemplateResponse(
        request,
        "settings_feedback.html",
        {
            "page_title": "Зворотний зв'язок",
            "user": user,
            # Токен не віддаємо в контекст — лише ознаку «збережено».
            "token_saved": bool((get_setting(db, "telegram_bot_token") or "").strip()),
            "chat_id": get_setting(db, "telegram_chat_id") or "",
            "push_enabled": (get_setting(db, "feedback_telegram_enabled") or "") == "1",
            "flash": flash,
        },
    )


@router.post("/settings/feedback")
async def save_feedback_settings(request: Request, db: Session = Depends(get_db)):
    require_settings_admin(request, db)
    form = await request.form()
    token = (form.get("telegram_bot_token") or "").strip()
    chat_id = (form.get("telegram_chat_id") or "").strip()
    enabled = "1" if form.get("feedback_telegram_enabled") else ""

    # Порожній токен = не міняти (він рендериться порожнім навмисно, як пароль).
    if token:
        set_setting(db, "telegram_bot_token", token)
    set_setting(db, "telegram_chat_id", chat_id)
    set_setting(db, "feedback_telegram_enabled", enabled)
    db.commit()

    request.session["feedback_settings_flash"] = {
        "kind": "success",
        "message": "Збережено.",
    }
    return RedirectResponse("/settings/feedback", status_code=303)


@router.post("/settings/feedback/bind")
def bind_feedback_chat(request: Request, db: Session = Depends(get_db)):
    """Спіймати chat_id останнього, хто написав боту (getUpdates), і зберегти.

    Бот не може написати першим — оператор пише боту /start, тисне цю кнопку."""
    require_settings_admin(request, db)
    from app.services.telegram import discover_chat_id

    chat_id, error = discover_chat_id(db)
    if chat_id is None:
        request.session["feedback_settings_flash"] = {
            "kind": "error",
            "message": error or "не вдалось знайти чат",
        }
    else:
        set_setting(db, "telegram_chat_id", chat_id)
        db.commit()
        request.session["feedback_settings_flash"] = {
            "kind": "success",
            "message": f"Прив'язано чат {chat_id}.",
        }
    return RedirectResponse("/settings/feedback", status_code=303)


@router.post("/settings/feedback/test")
def test_feedback_push(request: Request, db: Session = Depends(get_db)):
    """Надіслати тестове повідомлення в Telegram — перевірити токен і chat_id."""
    require_settings_admin(request, db)
    from app.services.telegram import get_bot_token, get_chat_id, _new_session, _send_message

    token = get_bot_token(db)
    chat_id = get_chat_id(db)
    if not token or not chat_id:
        request.session["feedback_settings_flash"] = {
            "kind": "error",
            "message": "Спершу збережіть токен і прив'яжіть чат.",
        }
        return RedirectResponse("/settings/feedback", status_code=303)

    try:
        session = _new_session()
        try:
            ok, err = _send_message(
                session, token, chat_id,
                "KuubMill: тестове повідомлення зворотного зв'язку ✓",
            )
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)

    request.session["feedback_settings_flash"] = {
        "kind": "success" if ok else "error",
        "message": "Надіслано — перевірте Telegram." if ok else f"Не вдалось: {err}",
    }
    return RedirectResponse("/settings/feedback", status_code=303)
