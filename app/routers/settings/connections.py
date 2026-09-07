"""Звʼязок із зовнішнім світом: Google Sheets, OAuth, IMAP, самоперевірка.

Самоперевірка стоїть тут же, бо крутить ті самі пробники (`_probe_imap_login`,
`open_spreadsheet`): рознесені по модулях, вони б із часом розійшлись. Тести
підміняють саме ці імена — патч мусить цілити в цей модуль.
"""

import json
import logging
import socket
import ssl
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from imap_tools import MailBox
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.__version__ import VERSION
from app.config import DB_PATH
from app.db import SessionLocal
from app.google_oauth import OAuthFlowError, parse_client_config, run_authorization_flow
from app.mail_reader import IMAP_HOST, IMAP_TIMEOUT_SECONDS
from app.monthly_backup import list_snapshots
from app.routers.deps import get_current_user, get_db, is_loopback_request, templates
from app.services.undo import log_action
from app.services.config_state import (
    imap_configured,
    sheets_access_error_message,
    sheets_configured,
)
from app.settings_store import (
    get_export_folder_path,
    get_google_oauth_client_json,
    get_imap_login,
    get_imap_password,
    get_technician_files_path,
    set_setting,
)
from app.sheets import measure_sheet_weight, open_spreadsheet, reset_sheets_cache
from app.update_check import get_known_update
from app.services.settings_nav import can_edit
from .common import require_settings_admin, require_settings_edit

logger = logging.getLogger(__name__)

router = APIRouter()


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
    if not can_edit(user, "imap"):
        raise HTTPException(status_code=403, detail="розділ доступний лише адміністратору")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    form = await request.form()
    login = (form.get("imap_login") or "").strip()
    password = (form.get("imap_password") or "").strip()
    # Empty password means "keep the saved one" — the field renders blank on
    # purpose (placeholder "•••• збережено"), so a save that only edits the login
    # must not wipe the stored password.
    changed = []
    if login:
        set_setting(db, "imap_login", login)
        changed.append("логін")
    if password:
        set_setting(db, "imap_password", password)
        changed.append("пароль")
    if changed:
        # У журнал іде ФАКТ зміни й ЩО саме змінили, ніколи значення:
        # розшифровані секрети не залишають settings_store (ревʼю 07.09.26, K.6).
        log_action(
            db, order=None, operator=user, action_type="settings",
            field="imap", note="змінено доступ до пошти: " + ", ".join(changed),
        )
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
    if not can_edit(user, "imap"):
        raise HTTPException(status_code=403, detail="розділ доступний лише адміністратору")
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

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
    if not can_edit(user, "sheets"):
        raise HTTPException(status_code=403, detail="розділ доступний лише адміністратору")
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
    require_settings_edit(request, db, "sheets")

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
    require_settings_edit(request, db, "sheets")
    set_setting(db, "google_oauth_refresh_token", "")
    set_setting(db, "google_auth_mode", "service_account")
    db.commit()
    reset_sheets_cache()
    return RedirectResponse("/settings?saved=1", status_code=303)


# One self-check probe may not wedge the run. Mirrors the reasoning behind
# mail_sync_service.MAIL_SYNC_DEADLINE_SECONDS: a half-open TLS socket can hang
# an IMAP/Sheets call indefinitely, and here that would stall a threadpool
# worker with the UI showing a spinner forever. Past the deadline the probe is
# abandoned (its thread is left to die on its own) and reported as a failure.
SELFCHECK_STEP_DEADLINE_SECONDS = 20


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
