"""Бекапи бази й копії Google-таблиці (CSV-знімки вкладок).

Два різні бекапи свідомо поруч: перший рятує CRM, другий — таблицю.
"""

from datetime import date, datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.requests import Request
from app.backup import (
    BackupFormatError,
    BackupIncompleteError,
    BackupPasswordError,
    create_backup,
    restore_backup,
)
from app.config import DB_PATH
from app.routers.deps import get_current_user, login_redirect, get_db, is_loopback_request
from app.services.undo import log_action
from app.settings_store import (
    set_sheet_backup_enabled,
    set_sheet_backup_interval_hours,
    SHEET_BACKUP_INTERVAL_DEFAULT_HOURS,
    SHEET_BACKUP_INTERVAL_MAX_HOURS,
    SHEET_BACKUP_INTERVAL_MIN_HOURS,
)
from app.sheet_backup import (
    build_zip as build_sheet_backup_zip,
    read_snapshot_bytes as read_sheet_snapshot_bytes,
    snapshot_all_tabs,
)
from .common import require_settings_edit

router = APIRouter()


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
        return login_redirect(request)
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
    # Генерується й одразу скачується — це не файл, який хтось читає назад за
    # іменем (на відміну від app/monthly_backup.py SNAPSHOT_PREFIX), тож без
    # старого варіанту сумісності: перейменувати можна одразу (CLAUDE.md §14).
    filename = f"kuubmill-backup-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
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
        return login_redirect(request)
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
    except BackupIncompleteError as exc:
        # Транзакцію вже відкочено — база лишилась така, як була. Адмінові
        # треба побачити ЧОМУ копія не лягла, а не порожній 500.
        request.session["settings_flash"] = {"kind": "error", "message": str(exc)}
        return RedirectResponse("/settings", status_code=303)

    # Слід у журналі — вже у ВІДНОВЛЕНІЙ базі: відновлення заміщує все, і без
    # запису «хто і коли залив копію» новий стан виглядав би так, ніби він був
    # тут завжди (ревʼю 07.09.26, K.6).
    log_action(
        db, order=None, operator=None, action_type="settings",
        field="backup.restore",
        note=(
            f"відновлено з копії: {counts.get('orders', 0)} робіт, "
            f"{counts.get('clients', 0)} клієнтів, {counts.get('users', 0)} операторів"
        ),
    )
    db.commit()

    request.session["settings_flash"] = {
        "kind": "success",
        "message": f"Відновлено: {counts.get('orders', 0)} робіт, {counts.get('clients', 0)} клієнтів, "
        f"{counts.get('users', 0)} операторів. Увійдіть повторно, якщо змінилися облікові дані.",
    }
    return RedirectResponse("/settings", status_code=303)


# ── Сирі знімки вкладок Google-таблиці (app/sheet_backup.py) ─────────────────
# Страховка для відновлення самої таблиці: CSV-копія кожної датованої вкладки,
# яку можна залити назад у Google. Керується адміном з розділу «Копії таблиці».


def _iso_to_tab_name(filename: str) -> str:
    """`2026-07-22.csv` → `22.07.26.csv` — щоб завантажений файл ліг назад у
    Google під тією ж назвою вкладки, що й раніше."""
    try:
        d = date.fromisoformat(filename[:-4])
        return d.strftime("%d.%m.%y") + ".csv"
    except (ValueError, TypeError):
        return filename


@router.post("/settings/sheets/config", response_class=HTMLResponse)
def save_sheet_backup_config(
    request: Request,
    enabled: str = Form(""),
    interval_hours: str = Form(str(SHEET_BACKUP_INTERVAL_DEFAULT_HOURS)),
    db: Session = Depends(get_db),
):
    """Зберегти вимикач і період авто-знімання вкладок."""
    require_settings_edit(request, db, "sheet-backup")
    try:
        hours = int((interval_hours or "").strip())
    except (ValueError, TypeError):
        request.session["settings_flash"] = {
            "kind": "error",
            "message": "Період має бути цілим числом годин.",
        }
        return RedirectResponse("/settings", status_code=303)
    if not (SHEET_BACKUP_INTERVAL_MIN_HOURS <= hours <= SHEET_BACKUP_INTERVAL_MAX_HOURS):
        request.session["settings_flash"] = {
            "kind": "error",
            "message": f"Період має бути від {SHEET_BACKUP_INTERVAL_MIN_HOURS} "
            f"до {SHEET_BACKUP_INTERVAL_MAX_HOURS} годин.",
        }
        return RedirectResponse("/settings", status_code=303)

    set_sheet_backup_enabled(db, enabled == "on")
    set_sheet_backup_interval_hours(db, hours)
    db.commit()
    request.session["settings_flash"] = {
        "kind": "success",
        "message": (
            f"Автокопії таблиці {'увімкнено' if enabled == 'on' else 'вимкнено'}, "
            f"період — кожні {hours} год."
        ),
    }
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/sheets/snapshot", response_class=HTMLResponse)
def snapshot_sheets_now(request: Request, db: Session = Depends(get_db)):
    """Зняти копію вкладок просто зараз (ручна кнопка). Читає нові й свіжі
    вкладки, старі вже зняті дні не перечитує — тож дешево навіть на проксі."""
    require_settings_edit(request, db, "sheet-backup")
    result = snapshot_all_tabs(db, DB_PATH)
    if result.error:
        request.session["settings_flash"] = {"kind": "error", "message": result.error}
    else:
        parts = [f"оновлено вкладок: {result.written}"]
        if result.skipped_empty:
            parts.append(f"порожніх пропущено: {result.skipped_empty}")
        if result.disappeared:
            parts.append(f"зникло з Google: {result.disappeared}")
        if result.held_shrink:
            parts.append(f"притримано (можливо обрізане читання): {result.held_shrink}")
        if result.failed:
            parts.append(f"помилок: {result.failed}")
        request.session["settings_flash"] = {
            "kind": "success" if not result.failed else "error",
            "message": "Знімок вкладок — " + ", ".join(parts) + ".",
        }
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/sheets/download/{filename}")
def download_sheet_snapshot(request: Request, filename: str, db: Session = Depends(get_db)):
    """Один CSV-знімок дня. Ім'я на віддачу — назва вкладки (`22.07.26.csv`)."""
    require_settings_edit(request, db, "sheet-backup")
    data = read_sheet_snapshot_bytes(DB_PATH, filename)
    if data is None:
        raise HTTPException(status_code=404, detail="Знімок не знайдено")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_iso_to_tab_name(filename)}"'},
    )


@router.get("/settings/sheets/download-month/{month}")
def download_sheet_month(request: Request, month: str, db: Session = Depends(get_db)):
    """ZIP усіх знімків одного місяця (`YYYY-MM`)."""
    require_settings_edit(request, db, "sheet-backup")
    try:
        y, m = month.split("-", 1)
        int(y)  # рік лише перевіряємо на числовість, значення не потрібне
        mm = int(m)
        if not (1 <= mm <= 12):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Невірний місяць")
    content, count = build_sheet_backup_zip(DB_PATH, month=month)
    if count == 0:
        raise HTTPException(status_code=404, detail="За цей місяць знімків немає")
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="google-tablytsia-{month}.zip"'},
    )


@router.get("/settings/sheets/download-all")
def download_sheet_all(request: Request, db: Session = Depends(get_db)):
    """ZIP усіх наявних знімків вкладок — повна страхувальна копія таблиці."""
    require_settings_edit(request, db, "sheet-backup")
    content, count = build_sheet_backup_zip(DB_PATH)
    if count == 0:
        raise HTTPException(status_code=404, detail="Знімків ще немає")
    stamp = datetime.now().strftime("%Y%m%d")
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="google-tablytsia-vse-{stamp}.zip"'},
    )
