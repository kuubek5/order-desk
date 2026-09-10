"""Головний екран налаштувань: рендер, збереження полів, пробник шляху.

Секрети приходять сюди формою й лягають у базу зашифрованими (CLAUDE.md §7):
у Jinja-контекст віддається лише ознака «задано», не саме значення.
"""

import uuid
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from app import sync_control
from app.business_day import set_rollover
from app.changelog import load_changelog
from app.services.handout_qc import qc_checklist_enabled
from app.services.health_snapshot import last_report
from app.config import DB_PATH, MAIL_ATTACHMENTS_PATH
from app.mail_spool import analyze_spool_cached
from app.backup_parts import PARTS as BACKUP_PARTS
from app.migration_files import summarize as migration_files_summary
from app.services.section_gate import sections_admin
from .blanks import blanks_context
from app.services.settings_nav import can_edit
from app.services.undo import log_action
from app.services.settings_status import build_slabs
from app.models import AppSetting, EmailMessage, MailFilterCategory, MailFilterRule, Order, User
from app.backup_mirror import mirror_status
from app.monthly_backup import list_snapshots
from app.routers.section_gate import blocked_response
from app.routers.deps import (
    get_current_user,
    login_redirect,
    get_db,
    is_loopback_request,
    templates,
    toast_response,
)
from app.routers.mail import _mail_filter_categories
from app.machine_portraits import portrait_version
from app.services import machines as machines_service
from app.services.config_state import imap_configured, sheets_configured
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
    get_furnace_background,
    get_furnace_vnc_password,
    get_machine_calibration_path,
    get_machine_vnc_password,
    get_setting,
    get_mail_download_all,
    get_notify_events,
    get_notify_position,
    get_notify_style,
    get_service_account_email,
    get_sheet_backup_enabled,
    get_sheet_backup_interval_hours,
    get_technician_files_path,
    set_setting,
    SHEET_BACKUP_INTERVAL_MAX_HOURS,
    SHEET_BACKUP_INTERVAL_MIN_HOURS,
)
from app.sheet_backup import list_snapshots as list_sheet_snapshots
from app.services.furnace import list_furnaces
from app.sheet_sync_service import SheetSyncError, summary_message, sync_google_sheets
from app.sync_control import MAIL_SYNC_INTERVAL_SECONDS, SHEET_SYNC_INTERVAL_SECONDS
from app.sync_heartbeat import sync_status_pair

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


def check_path_status(raw_path: str, *, write_probe: bool = True) -> dict[str, str]:
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

    if not write_probe:
        # Non-admins get the read-only answer. Creating and deleting a marker
        # file in an arbitrary path the caller typed turns this into a
        # write-anywhere oracle over every share the service account reaches
        # (audit 05.09.26, security M-1).
        return {"state": "success", "message": "Папку знайдено"}

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


def _sheet_snapshots_context() -> dict:
    """Знімки вкладок для розділу «Копії таблиці»: плаский список днів +
    зведення по місяцях (для кнопки «скачати місяць»). Читає лише файлову
    систему, до Google не ходить."""
    from app.services.formatting import uk_month_label

    snaps = list_sheet_snapshots(DB_PATH)
    months: dict[str, dict] = {}
    for s in snaps:
        y, m = s.month_ym.split("-")
        bucket = months.setdefault(
            s.month_ym,
            {"ym": s.month_ym, "label": uk_month_label(int(y), int(m)), "count": 0, "size_kb": 0.0},
        )
        bucket["count"] += 1
        bucket["size_kb"] += s.size_kb
    month_list = [months[k] for k in sorted(months, reverse=True)]
    return {
        "sheet_snapshots": snaps,
        "sheet_snapshot_months": month_list,
        "sheet_snapshot_total": len(snaps),
        "sheet_snapshot_disappeared": sum(1 for s in snaps if s.disappeared_at),
    }


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
        return login_redirect(request)

    # Розділ може бути зачинений адміністратором (Налаштування → Доступ до
    # розділів): не-адмін бачить екран-блокатор, адмін — сам розділ.
    blocked = blocked_response(request, db, user, "settings")
    if blocked is not None:
        return blocked
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

    context = {
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
        # Правила видачі: QC-чеклист перед «знайдено» (розділ «Ранкова видача»).
        "handout_qc": qc_checklist_enabled(db),
        # Popup-notification preferences («Спливаючі сповіщення»).
        "notify_style": get_notify_style(db),
        "notify_position": get_notify_position(db),
        "notify_events": get_notify_events(db),
        "notify_all": NOTIFY_EVENTS,
        # Заготовки: шлях, список до замовлення й готовий текст комірниці.
        # Тека тут НЕ читається — свіжість дає фоновий воркер або кнопка
        # «Перечитати»; похід у файлову систему з рендера налаштувань це
        # рівно те, від чого страждала видача.
        **blanks_context(db),
        "paths_set": paths_set,
        "operators_exist": operators_exist,
        # Чи заданий ПІН розділу «Виробіток» (значення не показуємо — лише
        # ознаку, як пароль). Порожньо = розділ відкритий.
        "vyrobitok_pin_set": bool(get_setting(db, "vyrobitok_pin")),
        # Розділи «в розробці / тестується» — керування станом (адмін).
        "sections_admin": sections_admin(db),
        "backup_available": backup_available,
        "monthly_snapshots": [
            {
                "name": p.name,
                "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
            }
            for p in list_snapshots(DB_PATH)
        ],
        # Друга копія на іншому носії (app/backup_mirror.py). Порожній шлях =
        # вимкнено. Плита розділу фарбується саме звідси, а не з кількості
        # файлів: файли можуть лежати з минулого року, поки копіювання давно
        # падає (аудит 08.09.26).
        "backup_mirror": mirror_status(db),
        # Сирі знімки вкладок Google-таблиці (app/sheet_backup.py).
        "sheet_backup_enabled": get_sheet_backup_enabled(db),
        "sheet_backup_interval_hours": get_sheet_backup_interval_hours(db),
        "sheet_backup_interval_min": SHEET_BACKUP_INTERVAL_MIN_HOURS,
        "sheet_backup_interval_max": SHEET_BACKUP_INTERVAL_MAX_HOURS,
        **_sheet_snapshots_context(),
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
        "machines": (_machines := machines_service.list_machines(db)),
        "machine_password_set": bool(get_machine_vnc_password(db)),
        # Тека калібрувальних кадрів: показуємо ДІЮЧУ (з урахуванням
        # типової) і окремо власну — у поле підставляється лише власна,
        # інакше «зберегти» перетворило б типову теку на прибиту цвяхами.
        "machine_calibration_path": get_machine_calibration_path(db),
        "machine_calibration_custom": get_setting(db, "machine_calibration_path") or "",
        # Стан збору кадрів (той самий фрагмент, що оновлює себе поллом).
        "calibration": machines_service.calibration_status(
            get_machine_calibration_path(db)
        ),
        # Версія (mtime) фото на верстат — для мініатюри в таблиці; None = нема.
        "machine_portrait_version": {m.id: portrait_version(m.id) for m in _machines},
        # Кешовано: обхід спулу з stat() на кожен файл по мережевій шарі не
        # має повторюватись на кожному відкритті /settings (M.5).
        "spool_report": (_spool_report := analyze_spool_cached(db, Path(MAIL_ATTACHMENTS_PATH))),
        # "Стан системи" flow map — honest, cheap counts (one scalar each).
        # No export-folder scan here; that's the heavy walk we keep off page load.
        # Звіт звірки після останнього оновлення (порожньо, поки оновлень не було).
        "update_health": last_report(db),
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
    }

    # Плити стану розділів (макет «Стенд»). Рахуються ПІСЛЯ контексту й з
    # нього ж: жодного власного джерела правди — інакше плита й тіло
    # розділу показували б різні числа (урок смуги печей).
    context["slabs"] = build_slabs(db, context)
    # Скільки файлів поїде в архіві переїзду — видно ДО кліку, щоб не качати
    # порожній zip і не гадати, чи там щось є (app/migration_files.py).
    context["migration_files"] = migration_files_summary()
    # Набори часткової копії — людські назви для груп таблиць; сам перелік
    # живе в app/backup_parts.py, щоб екран і роут не розійшлись.
    context["backup_parts"] = BACKUP_PARTS
    return templates.TemplateResponse(request, "settings.html", context)


@router.post("/settings", response_class=HTMLResponse)
async def post_settings(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)
    # Ця форма пише СЕКРЕТИ (пароль IMAP, service-account JSON, шляхи до шар).
    # Такі дії — лише за фізичним ПК, як і решта «керує самою машиною»
    # (ревʼю 07.09.26, K.4). Права на КОЖНЕ поле лишаються польовими, нижче:
    # роут і далі відкритий операторові, тож у сторожі гейтів він лишається
    # у списку винятків — тут додано саме loopback, не роль.
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")
    is_admin = user.role == "адмін"

    form = await request.form()
    action = form.get("action", "save")
    # Field-level enforcement, not just a hidden card: рендер форми показує
    # рівно те, що людині можна, але саморобний POST міг би принести чуже
    # поле — тому кожне поле звіряється з правами на ЙОГО розділ
    # (`app/services/settings_nav.py`), а не з роллю напряму. Ключ без розділу
    # (нове поле, яке забули підписати) лишається адмінським.
    touched: list[str] = []
    for field in SETTING_FIELDS:
        allowed = (
            is_admin
            or field.key in OPERATOR_EDITABLE_KEYS
            or (field.section and can_edit(user, field.section))
        )
        if not allowed:
            continue
        raw = form.get(field.key)
        if raw is None:
            # ПОЛЯ НЕ БУЛО В ЦІЙ ФОРМІ — не чіпаємо його. Екран налаштувань
            # має ТРИ окремі <form>, і всі три шлють POST сюди: Google, IMAP,
            # шляхи. Раніше тут стояло form.get(key, ""), тобто «відсутнє»
            # ставало «порожнє», а порожнє для CLEARABLE означає «стерти» —
            # тож збереження однієї секції мовчки витирало поля двох інших.
            # Бойовий випадок 03.09.26: оператор зберіг шлях до проєктів
            # Sum3D → у ту саму мить зник Google Sheet ID («таблиця не може
            # синхронізуватися») і шлях до export («0 тек у сховищі» на
            # видачі). Один клік — три втрачені налаштування.
            continue
        value = raw.strip()
        if field.key == "google_sheet_id":
            # Operators paste the whole address-bar URL; store the bare id.
            value = extract_sheet_id(value)
        # Порожнє = «не міняти» ЛИШЕ для секретів: їхнє поле рендериться
        # порожнім навмисно (placeholder «збережено»), тож повторний сабміт
        # не має їх стерти. Для шляхів і Sheet ID порожнє = «прибрати»,
        # інакше помилковий мережевий шлях, що вішає видачу, неможливо було
        # зняти — а тост при цьому рапортував «Збережено». Очищення лишається
        # можливим саме тому, що поле в формі Є — просто його стерли руками.
        if value or field.key in CLEARABLE_SETTING_KEYS:
            set_setting(db, field.key, value)
            touched.append(field.key)
    if touched:
        # У журнал іде перелік КЛЮЧІВ, ніколи значень: серед них пароль IMAP і
        # service-account JSON, а розшифровані секрети не залишають
        # settings_store (ревʼю 07.09.26, K.6).
        log_action(
            db, order=None, operator=user, action_type="settings",
            field="settings", note="змінено налаштування: " + ", ".join(sorted(touched)),
        )
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

    if action == "save_and_sync" and sync_control.is_paused():
        # Той самий гейт, що й ручний "/sheets/sync" (app/routers/queue.py) і
        # день-синк у Виробітку: пауза зупиняє ЙОГО читання й запис, «зберегти
        # й синхронізувати» не мало лишатись дірою повз неї (F6, аудит
        # 06.09.26).
        message = "Синхронізацію призупинено. Зніміть паузу, щоб синхронізувати."
        if hx:
            return toast_response(message, kind="error")
        request.session["sync_flash"] = {"kind": "error", "message": message}
        return RedirectResponse("/", status_code=303)

    if action == "save_and_sync":
        try:
            # Не на event loop (CLAUDE.md §14): gspread ходить у мережу й
            # блокує потік. Роут лишається `async def` лише заради
            # `await request.form()` вище — важка частина винесена сюди.
            summary = await run_in_threadpool(sync_google_sheets, db)
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
    # Читати шлях може будь-хто, хто ввійшов: це не секрет, а «чи бачу я цю
    # теку з цього ПК». А от ЗАПИС (створити й прибрати файл-маркер за
    # довільним шляхом із форми) лишається адмінським НАВІТЬ ПІСЛЯ того, як
    # оператор отримав право редагувати «Шляхи папок»: це не редагування
    # налаштування, а запис у будь-яку мережеву шару, куди дістає служба
    # KuubMill (audit 05.09.26, security M-1). Право на розділ таку дію не
    # покриває — тому тут стоїть роль, а не can_edit.
    if not is_loopback_request(request):
        raise HTTPException(status_code=403, detail="дія доступна лише на цьому комп'ютері")

    if kind == "sum3d":
        raw_path = sum3d_projects_path
    elif kind == "export":
        raw_path = export_folder_path
    else:
        raw_path = technician_files_path
    result = check_path_status(raw_path or "", write_probe=user.role == "адмін")

    return templates.TemplateResponse(
        request, "_settings_check_result.html", {"result": result}
    )
