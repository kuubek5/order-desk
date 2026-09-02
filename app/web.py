from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
import logging
import os
import time
from pathlib import Path
from threading import (
    Event,
    Thread,
)
from time import monotonic
from typing import Callable

from fastapi import (
    FastAPI,
)
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from app.__version__ import VERSION
from app.config import (
    DB_PATH,
    SESSION_SECRET_KEY,
)
from app.db import Base, SessionLocal, engine
from app.monthly_backup import ensure_monthly_snapshot
from app.export_scanner import list_export_client_names_cached
from app import sync_control
from app.sync_control import (
    MAIL_SYNC_INITIAL_DELAY_SECONDS,
    MAIL_SYNC_INTERVAL_SECONDS,
    SHEET_SYNC_INITIAL_DELAY_SECONDS,
    hot_extra_days as _hot_extra_days,
)
from app.sync_heartbeat import record_heartbeat as _record_sync_heartbeat
from app.services.config_state import (
    imap_configured as _imap_configured,
    sheets_configured as _sheets_configured,
)
from app.sync_control import get_sync_speed
from app.license import get_license_status
from app.mail_sync_service import (
    run_sync_owned_session as _run_mail_sync_owned_session,
    MailSyncBusyError,
    MailSyncError,
)
from app.material_catalog import (
    backfill_orders,
    ensure_seeded,
)
from app.runtime import data_dir, resource_path
from app.routers.auth import router as auth_router
from app.routers.clients import router as clients_router
from app.routers.handout import HANDOUT_DAY_WINDOW
from app.routers.handout import router as handout_router
from app.routers.settings import router as settings_router
from app.routers.mail import router as mail_router
from app.routers.orders import router as orders_router
from app.routers.queue import router as queue_router
from app.routers.archive import router as archive_router
from app.routers.stats import router as stats_router
from app.routers.stl import router as stl_router
from app.routers.shift import router as shift_router
from app.routers.furnace import router as furnace_router
from app.routers.machines import router as machines_router
from app.routers.feedback import router as feedback_router
from app.services.furnace import (
    POLL_INTERVAL_SECONDS as FURNACE_POLL_INTERVAL_SECONDS,
    is_configured as _furnaces_configured,
    poll_all as _poll_furnaces,
    prune_readings as _prune_furnace_readings,
)
from app.services.machines import (
    POLL_INTERVAL_SECONDS as MACHINE_POLL_INTERVAL_SECONDS,
    is_configured as _machines_configured,
    poll_all as _poll_machines,
)
from app.shift_images import prune_shift_images
from app.routers.deps import templates
from app.services.order_dates import parse_sheet_tab as _parse_sheet_tab
from app.services.handout import (
    handout_client_matches as _handout_client_matches,
    handout_day_options as _handout_day_options,
    handout_eligible_orders as _handout_eligible_orders,
    handout_not_before as _handout_not_before,
    handout_select_day as _handout_select_day,
    matched_folders as _matched_folders,
    scan_export_for_clients as _scan_export_for_clients,
    scan_export_latest_for_clients as _scan_export_latest_for_clients,
)
from app.services.sheet_writeback import (
    sheet_writeback_pool as _sheet_writeback_pool,
    warm_sheet_writeback as _warm_sheet_writeback,
)
from app.settings_store import get_export_folder_path
from app.sheet_sync_service import (
    SheetSyncBusyError,
    SheetSyncError,
    sync_hot_tab,
    sync_sheets_background,
)
from app.update_check import _update_check_worker

logger = logging.getLogger(__name__)
# Автоматизація Провідника Windows винесена в app/platform_windows.py
# (Крок 2 розбиття web.py). Імпортуємо під старими іменами, щоб роути й
# тести (які монкіпатчать web._open_folder_in_explorer) працювали без змін.


def _mail_sync_tick(db: Session) -> None:
    """One background IMAP sync attempt: gated on IMAP being configured
    (an unconfigured sync is not attempted, and correctly stays "не
    налаштовано" rather than touching the heartbeat). Records a
    SyncHeartbeat for success or either well-known failure mode
    (MailSyncBusyError / MailSyncError); any other exception is left to
    propagate to _mail_sync_worker's own generic handler below, which
    records its own failure heartbeat — same three-way dispatch the
    original inline try/except used, just factored out so it is directly
    callable from tests without threads/Event. See SyncHeartbeat's
    docstring for why this exists alongside SyncLog.

    `db` is used only for the configured-gate read; the sync itself runs on
    a session it owns (see _run_mail_sync_owned_session) so a watchdog
    timeout can't leave this per-tick session in the zombie's hands.
    """
    if not _imap_configured(db):
        return
    try:
        _run_mail_sync_owned_session(trigger="background")
    except MailSyncBusyError:
        _record_sync_heartbeat("mail", status="skipped")
        return
    except MailSyncError as exc:
        logger.warning("Background mail sync failed: %s", exc)
        _record_sync_heartbeat("mail", status="error", error_message=str(exc))
        return
    _record_sync_heartbeat("mail", status="ok")


def _mail_sync_worker(stop_event: Event) -> None:
    """Poll IMAP without occupying the web request loop or delaying shutdown."""
    if stop_event.wait(MAIL_SYNC_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                _mail_sync_tick(db)
        except Exception:
            logger.exception("Unexpected background mail sync failure")
            _record_sync_heartbeat(
                "mail", status="error", error_message="Неочікувана помилка синхронізації пошти"
            )

        stop_event.wait(MAIL_SYNC_INTERVAL_SECONDS)


def _sheet_sync_tick(db: Session) -> None:
    """One background Google Sheets sync attempt — same gate/dispatch/testing
    rationale as _mail_sync_tick above, for SheetSyncBusyError / SheetSyncError."""
    if not _sheets_configured(db):
        return
    try:
        sync_sheets_background(db)
    except SheetSyncBusyError:
        _record_sync_heartbeat("sheet", status="skipped")
        return
    except SheetSyncError as exc:
        logger.warning("Background sheet sync failed: %s", exc)
        _record_sync_heartbeat("sheet", status="error", error_message=str(exc))
        return
    _record_sync_heartbeat("sheet", status="ok")


def _sheet_hot_tick(db: Session) -> None:
    """One fast-lane attempt at the current day's tab (sync_hot_tab). Errors
    are logged but do NOT flip the heartbeat to error: this runs every ~15s,
    and a transient proxy blip here would flap the UI state that the full
    sync (with its retries and audit trail) is the real owner of."""
    if not _sheets_configured(db):
        return
    try:
        summary = sync_hot_tab(db, extra_days=_hot_extra_days())
    except SheetSyncError as exc:
        logger.warning("Hot-tab sheet sync failed: %s", exc)
        return
    if summary is None:
        return  # lock busy (full sync in flight) or today's tab not created yet
    _record_sync_heartbeat("sheet", status="ok")
    if summary.created or summary.updated or summary.deleted:
        logger.info(
            "Hot-tab sync %s: created %d, updated %d, deleted %d",
            summary.tab_names[0],
            summary.created,
            summary.updated,
            summary.deleted,
        )


def _sheet_sync_worker(stop_event: Event) -> None:
    """Poll Google Sheets without occupying the web request loop or delaying
    shutdown — same shape as _mail_sync_worker above. Table rows are entered
    by trusted internal staff (technologists/admins), not free-text clients,
    so unlike email there is no per-row guess to review before it becomes an
    Order: sync_tab already writes directly. Automating the periodic pull
    just removes the "someone has to remember to click Синхронізувати" step.

    Two-speed loop: the expensive full sync (worksheets listing + 3-day
    window) runs every SHEET_SYNC_INTERVAL_SECONDS; in between, the cheap
    hot-tab read of today's tab runs every SHEET_SYNC_HOT_INTERVAL_SECONDS so
    current-day edits land almost live. Both run on THIS one thread, sharing
    its warm per-thread spreadsheet/worksheet cache (app/sheets.py)."""
    if stop_event.wait(SHEET_SYNC_INITIAL_DELAY_SECONDS):
        return

    next_full = 0.0  # first iteration always does a full sync
    while not stop_event.is_set():
        speed = get_sync_speed()  # live: the UI switch changes the next tick
        # Paused: touch the sheet in neither direction. Keep looping (cheaply)
        # so resume takes effect within one tick; force a full sync on resume so
        # the first thing after a pause is a complete re-read of the table.
        if sync_control.is_paused():
            next_full = 0.0
            stop_event.wait(speed["hot"])
            continue
        run_full = monotonic() >= next_full
        try:
            with SessionLocal() as db:
                if run_full:
                    _sheet_sync_tick(db)
                    next_full = monotonic() + speed["full"]
                else:
                    _sheet_hot_tick(db)
        except Exception:
            logger.exception("Unexpected background sheet sync failure")
            _record_sync_heartbeat(
                "sheet", status="error", error_message="Неочікувана помилка синхронізації таблиці"
            )
            if run_full:
                next_full = monotonic() + speed["full"]

        stop_event.wait(speed["hot"])


# Monthly DB snapshot: check this often whether last month's archive exists
# yet (see app/monthly_backup.py for why it's a poll, not a midnight cron —
# the lab PC is off at midnight). 6h means the archive appears within hours of
# the first launch in a new month, and the check is a single cheap file-exists
# the rest of the time.
MONTHLY_BACKUP_INTERVAL_SECONDS = 6 * 60 * 60
MONTHLY_BACKUP_INITIAL_DELAY_SECONDS = 30


def _monthly_backup_tick() -> None:
    """One archive-if-missing attempt. Never raises — a snapshot failure must
    not touch the running app; it just retries on the next tick."""
    try:
        created = ensure_monthly_snapshot(engine, DB_PATH)
        if created is not None:
            logger.info("Monthly backup written: %s", created.name)
    except Exception:
        logger.exception("Monthly DB snapshot failed")


def _monthly_backup_worker(stop_event: Event) -> None:
    """Create the previous month's DB snapshot at the first opportunity after
    the month rolls over, then idle-check every interval — same
    thread/shutdown shape as the sync workers above."""
    if stop_event.wait(MONTHLY_BACKUP_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        _monthly_backup_tick()
        stop_event.wait(MONTHLY_BACKUP_INTERVAL_SECONDS)


# Скріншоти записок передачі зміни живуть 6 місяців (рішення власника), текст
# записки — назавжди. Раз на добу досить: різниця між «прибрано сьогодні» і
# «прибрано завтра» тут нульова, а тік — один SELECT і обхід невеликої теки.
SHIFT_IMAGES_PRUNE_INTERVAL_SECONDS = 24 * 60 * 60
SHIFT_IMAGES_PRUNE_INITIAL_DELAY_SECONDS = 90


def _shift_images_prune_tick() -> None:
    """Одна спроба прибирання. Ніколи не кидає — збій прибирання не має
    чіпати застосунок, наступний тік спробує знову."""
    try:
        db = SessionLocal()
        try:
            removed, freed = prune_shift_images(db)
            if removed:
                db.commit()
                logger.info(
                    "Прибрано скріншотів зміни: %s, звільнено %s байт", removed, freed
                )
            else:
                db.rollback()
        finally:
            db.close()
    except Exception:
        logger.exception("Прибирання скріншотів зміни не вдалось")


def _shift_images_prune_worker(stop_event: Event) -> None:
    if stop_event.wait(SHIFT_IMAGES_PRUNE_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        _shift_images_prune_tick()
        stop_event.wait(SHIFT_IMAGES_PRUNE_INTERVAL_SECONDS)


# ── Ретрай Telegram-пуша зворотного зв'язку ─────────────────────────────────
# Звернення завжди в базі; пуш — окремий крок. Якщо мережа лягла на момент
# створення (у цеху TLS-проксі рве зовнішні з'єднання), тут дошлемо. Дешевий
# COUNT + вихід, коли слати нема чого або пуш вимкнено.
FEEDBACK_RETRY_INITIAL_DELAY_SECONDS = 45.0
FEEDBACK_RETRY_INTERVAL_SECONDS = 120.0


def _feedback_retry_tick() -> None:
    from app.services.feedback import flush_pending_pushes

    with SessionLocal() as db:
        try:
            flush_pending_pushes(db)
        except Exception:
            logger.exception("feedback push retry tick failed")


def _feedback_push_retry_worker(stop_event: Event) -> None:
    if stop_event.wait(FEEDBACK_RETRY_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        _feedback_retry_tick()
        stop_event.wait(FEEDBACK_RETRY_INTERVAL_SECONDS)


# ── Печі спікання ───────────────────────────────────────────────────────────
# Кадр табло раз на кілька секунд. Це ЧИТАННЯ і тільки читання: у застосунку
# немає коду, який шле печі байт вводу (див. app/furnace_vnc.py). Керування
# піччю свідомо лишається людині коло печі.
FURNACE_INITIAL_DELAY_SECONDS = 10.0
# Прибирання старих показань — раз на добу, разом із першим тіком нової доби.
FURNACE_PRUNE_INTERVAL_SECONDS = 24 * 60 * 60


def _furnace_tick(db: Session) -> None:
    """Один прохід по всіх налаштованих печах.

    Ґейт на «чи налаштовано» стоїть ТУТ, а не в poll_all: без нього кожні
    кілька секунд ходив би зайвий запит у налаштування на машині, де печей
    немає взагалі.
    """
    if not _furnaces_configured(db):
        return
    _poll_furnaces(db)


def _furnace_worker(stop_event: Event) -> None:
    """Опитувати печі, не займаючи цикл запитів і не затримуючи вимкнення —
    та сама форма, що в синків пошти й таблиці вище.

    Помилка знімка (піч вимкнена на ніч, обірваний кабель) — нормальний стан і
    гаситься всередині poll_target; сюди долітає лише те, чого ми не
    передбачили, і воно не має вбивати потік.
    """
    if stop_event.wait(FURNACE_INITIAL_DELAY_SECONDS):
        return
    next_prune = 0.0
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                _furnace_tick(db)
                if monotonic() >= next_prune:
                    _prune_furnace_readings(db)
                    next_prune = monotonic() + FURNACE_PRUNE_INTERVAL_SECONDS
        except Exception:
            logger.exception("Неочікуваний збій опитування печей")
        stop_event.wait(FURNACE_POLL_INTERVAL_SECONDS)


# Верстати — той самий контракт, що печі: читання і тільки читання екрана
# по VNC (app/furnace_vnc.py фізично не вміє слати ввід). Кадри рідші за
# пічні: framebuffer 1080p учетверо більший, а верстатів — до десяти.
MACHINE_INITIAL_DELAY_SECONDS = 15.0


def _machine_worker(stop_event: Event) -> None:
    if stop_event.wait(MACHINE_INITIAL_DELAY_SECONDS):
        return
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                if _machines_configured(db):
                    _poll_machines(db)
        except Exception:
            logger.exception("Неочікуваний збій опитування верстатів")
        stop_event.wait(MACHINE_POLL_INTERVAL_SECONDS)


EXPORT_WARM_INITIAL_DELAY_SECONDS = 20.0
EXPORT_WARM_INTERVAL_SECONDS = 120.0


def _export_warm_worker(stop_event: Event) -> None:
    """Тримати кеш обходу export теплим, щоб екран видачі відкривався одразу.

    Обхід сховища коштує рівно стільки, скільки коштує — виміряно 27.08.26 на
    бойовій шарі: 33 мс/запис послідовно, 8.5 мс у 16 потоків, а на екрані
    ~2525 записів. Питання лише в тому, ХТО за це платить. Досі платив
    оператор, який відкрив видачу (80с очікування). Тепер платить фон, а
    оператор отримує вже готове.

    Кеш сканера (`app/export_scanner.py`) віддає протухле одразу й оновлює
    його у фоні, тож інтервал більший за TTL — це не зайве очікування, а
    лише вік даних, і для тек із роботами він неістотний.

    Ключі кешу мусять збігатися з тими, що рахує сам екран, — тому обидва
    йдуть через ті самі помічники (`_handout_*`). Інакше прогрів наповнить
    кеш під іншим ключем, і на екрані нічого не зміниться."""
    if stop_event.wait(EXPORT_WARM_INITIAL_DELAY_SECONDS):
        return

    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                export_warm_once(db)
        except Exception:  # noqa: BLE001 — фоновий прогрів не валить застосунок
            logger.exception("Фоновий прогрів export не вдався")
        if stop_event.wait(EXPORT_WARM_INTERVAL_SECONDS):
            return


def export_warm_once(db: Session) -> int:
    """Один прохід прогріву. Повертає кількість прогрітих тек."""
    root = Path(get_export_folder_path(db))
    folder_names = list_export_client_names_cached(root)
    if not folder_names:
        return 0
    all_eligible = _handout_eligible_orders(db, date.today())
    day_options = _handout_day_options(all_eligible)
    # Гріємо ВСІ дні видимого вікна чіпів, а не лише дефолтний. Ключ кешу
    # обходу включає межу за датою (not_before), і вона своя в кожного дня —
    # тому прогрів одного дефолтного дня лишав сусідні чіпи холодними, і
    # кожен клік по «вчора» ішов у синхронний SMB-обхід прямо в запиті
    # (скарга власника 31.08.26: «довго переходить між вкладками»).
    # «Усі дні» свідомо НЕ гріємо: це повний обхід сотень клієнтів (бойовий
    # лог: 511 с) кожні дві хвилини — дорожче, ніж рідкий клік по «усі».
    default_day = _handout_select_day(day_options, "")
    if default_day is not None:
        size = min(HANDOUT_DAY_WINDOW, len(day_options))
        anchor = day_options.index(default_day)
        start = max(0, min(anchor - size // 2, len(day_options) - size))
        warm_days = day_options[start:start + size]
    else:
        warm_days = []

    started = time.monotonic()
    total_folders = 0
    total_rows = 0
    for day in warm_days or [None]:
        eligible = all_eligible
        if day is not None:
            eligible = [o for o in eligible if _parse_sheet_tab(o.sheet_tab) == day]
        if not eligible:
            continue
        not_before = _handout_not_before(eligible)
        client_names = {o.client_name for o in eligible if o.client_name}
        folders = _matched_folders(_handout_client_matches(db, client_names, folder_names))
        scanned = _scan_export_for_clients(root, folders, not_before)
        # Той самий запасний шлях, що й на екрані — інакше перший, хто
        # відкриє видачу, платив би за нього сам.
        empty = {name: folder for name, folder in folders.items() if not scanned.get(name)}
        scanned.update(_scan_export_latest_for_clients(root, empty))
        total_folders += len(folders)
        total_rows += sum(len(v) for v in scanned.values())
    logger.info(
        "Export prewarm: %d дн., %d тек, %d записів, %.2fс",
        len(warm_days),
        total_folders,
        total_rows,
        time.monotonic() - started,
    )
    return total_folders


@dataclass
class _BackgroundWorker:
    """Один фоновий daemon-потік і його вимикач. Раніше кожен воркер
    заводився в lifespan вручну п'ятьма однаковими блоками start/stop —
    легко було проґавити один при зупинці. Тепер це список, а не копіпаст:
    видно всі фонові процеси в одному місці, і додати новий (напр. майбутній
    моніторинг печей) — один рядок.

    Логіку самих воркерів НЕ чіпаємо — вона лишається у web.py; це лише
    впорядкування їхнього життєвого циклу (перший, найбезпечніший крок
    розбиття, див. ARCHITECTURE_PLAN.md)."""
    name: str
    target: Callable
    stop_event: Event = field(default_factory=Event)
    thread: Thread | None = None

    def start(self) -> None:
        self.thread = Thread(
            target=self.target, args=(self.stop_event,), name=self.name, daemon=True
        )
        self.thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.environ.get("KUUBMILL_SCHEMA_MANAGED") != "1":
        Base.metadata.create_all(engine)
    # Seed the material catalog and classify any still-unresolved orders once at
    # boot. Idempotent and cheap; covers both the create_all path (no migration
    # seed) and the first boot after the 0005 upgrade backfills existing rows.
    # Never let it block startup.
    with SessionLocal() as db:
        try:
            ensure_seeded(db)
            backfill_orders(db)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Material catalog seed/backfill at startup failed")

    # Warm the sheet write-back worker's cache (open the spreadsheet once) so the
    # very first operator edit reflects in the sheet in ~3s instead of ~40s. Best
    # effort — skips silently if the sheet isn't configured or is unreachable.
    _sheet_writeback_pool.submit(_warm_sheet_writeback)

    workers = [
        _BackgroundWorker("order-desk-mail-sync", _mail_sync_worker),
        _BackgroundWorker("order-desk-sheet-sync", _sheet_sync_worker),
        _BackgroundWorker("order-desk-update-check", _update_check_worker),
        _BackgroundWorker("order-desk-monthly-backup", _monthly_backup_worker),
        _BackgroundWorker("order-desk-export-warm", _export_warm_worker),
        _BackgroundWorker("order-desk-shift-images-prune", _shift_images_prune_worker),
        _BackgroundWorker("order-desk-furnace", _furnace_worker),
        _BackgroundWorker("order-desk-machines", _machine_worker),
        _BackgroundWorker("kuubmill-feedback-retry", _feedback_push_retry_worker),
    ]
    for w in workers:
        w.start()
    try:
        yield
    finally:
        for w in workers:
            w.stop()


app = FastAPI(title="KuubMill", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    same_site="strict",
    https_only=False,  # Loopback-only HTTP; no network listener is opened.
    max_age=8 * 60 * 60,
)


# ── Сторінки помилок ──────────────────────────────────────────────────────
# До цього їх не було: FastAPI віддавав голий «Internal Server Error» чорним
# текстом на білому — оператор біля верстата бачив зламаний застосунок без
# назви, без виходу назад і без натяку, що робити.
#
# Текст винятку у відповідь НЕ йде: він може містити шляхи, фрагменти рядків
# таблиці й імена клієнтів. У лог — так, на екран — ні.

_ERROR_COPY = {
    404: ("Сторінки немає", "Можливо, роботу вже прибрали з черги або посилання застаріло."),
    403: ("Доступ закрито", "Ця дія доступна лише адміністратору або лише з цього комп'ютера."),
    409: ("Уже оброблено", "Хтось встиг зробити це раніше — оновіть сторінку, щоб побачити свіжий стан."),
    500: ("Щось пішло не так", "Помилка на боці застосунку. Дані не втрачені — спробуйте ще раз."),
}


def _error_page(request: Request, code: int) -> Response:
    title, message = _ERROR_COPY.get(code, _ERROR_COPY[500])
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "code": code,
            "title": title,
            "message": message,
            "log_path": str(data_dir() / "logs" / "kuubmill.log"),
        },
        status_code=code,
    )


def _wants_html(request: Request) -> bool:
    """HTMX-фрагменти й API лишають звичайну JSON/текстову відповідь: сторінка
    помилки, вставлена в середину таблиці, зіпсувала б розмітку черги."""
    if request.headers.get("HX-Request"):
        return False
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code in _ERROR_COPY and _wants_html(request):
        return _error_page(request, exc.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Необроблена помилка на %s %s", request.method, request.url.path)
    if _wants_html(request):
        return _error_page(request, 500)
    raise exc


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    """Log only user-visible stalls, without adding noise for normal requests."""
    started_at = monotonic()
    try:
        return await call_next(request)
    finally:
        duration = monotonic() - started_at
        if duration >= 1.0:
            logger.warning(
                "Slow request: %s %s took %.3fs",
                request.method,
                request.url.path,
                duration,
            )


# Paths that must work with zero license, zero session, zero DB assumptions:
# /health is polled by the release-workflow smoke test (.github/workflows/release.yml)
# before any activation happens, and /static serves the CSS/JS the /license
# screen itself needs to render.
_LICENSE_EXEMPT_PATH_PREFIXES = ("/static/",)
_LICENSE_EXEMPT_PATHS = ("/health", "/license")


@app.middleware("http")
async def no_store_html(request: Request, call_next):
    """Stop the browser caching dynamic HTML pages.

    Static assets already cache-bust via static_ver()'s ?v=<mtime>, but the
    TemplateResponse HTML itself carried no cache header, so after an app
    upgrade a browser could keep serving a stale page (e.g. mail triage with
    the old floating STL popup) until a hard refresh. HTML is per-session and
    changes constantly (queue, mail) — it must never be cached; static files
    (their own content-type, not text/html) are untouched and stay cacheable.
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.middleware("http")
async def license_gate(request: Request, call_next):
    """Block the entire application — even /setup and /login — without a valid license.

    Runs outermost (defined after, so registered last / wraps everything else,
    see Starlette's LIFO middleware stack) and reads its own DB session rather
    than depending on get_db, since dependency injection isn't available here.
    """
    path = request.url.path
    if path in _LICENSE_EXEMPT_PATHS or path.startswith(_LICENSE_EXEMPT_PATH_PREFIXES):
        return await call_next(request)

    db = SessionLocal()
    try:
        status = get_license_status(db)
    finally:
        db.close()

    if not status.valid:
        return RedirectResponse("/license", status_code=303)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(resource_path("app/static"))), name="static")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Process-level probe without exposing configuration or mutating the DB.

    Reports the running build version so support and the update watchdog's
    post-relaunch check can confirm *which* build answered, not merely that
    one did — the version is not a secret and needs no auth here."""
    return {"status": "ok", "version": VERSION}


# Вхід, ліцензія і кабінет оператора живуть в app/routers/auth.py.
app.include_router(auth_router)


# Черга робіт (основний екран) і ручні дії над синхронізацією живуть
# в app/routers/queue.py.
app.include_router(queue_router)


# Паспорт роботи, дії над нею, скасування і журнал живуть
# в app/routers/orders.py.
app.include_router(orders_router)


# Архів («Хроніка») живе в app/routers/archive.py.
app.include_router(archive_router)


# Екран видачі живе в app/routers/handout.py.
app.include_router(handout_router)


# STL-прев'ю живе в app/routers/stl.py. include_router стоїть саме тут, де
# ці роути й були оголошені, — порядок реєстрації в застосунку не змінюється.
app.include_router(stl_router)


# Статистика живе в app/routers/stats.py.
app.include_router(stats_router)


# Екран «Клієнти» живе в app/routers/clients.py.
app.include_router(clients_router)


# Налаштування (адмін) живуть в app/routers/settings.py.
app.include_router(settings_router)


# Тріаж пошти й фільтри живуть в app/routers/mail.py.
app.include_router(mail_router)


# Дошка передачі зміни живе в app/routers/shift.py.
app.include_router(shift_router)


# Екран печей (тільки перегляд) живе в app/routers/furnace.py.
app.include_router(furnace_router)
# Верстати — живі кадри екранів RemiCORE, дзеркало пічного модуля.
app.include_router(machines_router)
# Форма зворотного зв'язку — приймання звернень + адмін-стрічка «Вхідні».
app.include_router(feedback_router)
