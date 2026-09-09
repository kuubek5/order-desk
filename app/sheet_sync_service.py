"""Application service for importing dated Google Sheets tabs into the DB."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from collections import Counter
import json
import logging
import re
import time
from threading import Lock, Thread

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import sync_control
from app import log_throttle
from app.business_day import business_today, canonical_tab_title, utc_now
from app.db import SessionLocal
from app.models import Order, SyncLog
from app.parser import HEADER_ROWS, header_mismatches, parse_rows, unimported_work_rows
from app.sheet_colors import fetch_row_fills
from app.settings_store import (
    get_google_service_account_json,
    get_google_sheet_id,
    get_setting,
    set_setting,
)
from app.sheets import (
    api_calls_last_minute,
    call_with_retry,
    get_worksheet_by_name,
    open_spreadsheet,
    quota_is_tight,
    tab_name_for,
)
from app.services.formatting import pluralize_uk
from app.services.order_dates import parse_sheet_tab
from app.sync import sync_tab
from app.sync_heartbeat import record_agreement


logger = logging.getLogger(__name__)

_DATE_TAB_RE = re.compile(r"^\d{2}\.\d{2}\.\d{2}$")
_INITIAL_LOOKBACK_DAYS = 30


class SheetSyncError(RuntimeError):
    """Safe, user-displayable Google Sheets synchronization error."""


class SheetSyncConfigurationError(SheetSyncError):
    """Raised when required Google Sheets settings are missing or invalid."""


class SheetSyncBusyError(SheetSyncError):
    """Raised when another manual/background synchronization owns the lock."""


class SheetSyncPausedError(SheetSyncError):
    """Синк на паузі — читання таблиці зупинено так само, як і запис.

    Пауза існує рівно для одного: «не чіпайте таблицю зараз» (адміністратор
    щось у ній перебирає, техніки масово правлять день). Досі вона гальмувала
    лише ЗАПИС, тож фонові тіки далі читали й далі рухали чергу — включно з
    архівацією робіт за рядками, які саме перебирають. Тепер гейт стоїть у
    самих функціях читання, а не в роутах: місць виклику багато, і кожне нове
    інакше знову ходило б повз паузу.
    """


_sync_lock = Lock()
# How long a MANUAL sync waits for the lock before giving up — long enough to
# outlast one hot-tab tick (~3s warm), short enough that a click during a real
# full sync still errors out promptly instead of hanging the request.
_MANUAL_LOCK_WAIT_SECONDS = 10.0


def is_sheet_sync_running() -> bool:
    """True while ANY sheet sync (periodic tick, manual button, or the
    full-history import) currently holds the lock — the live «syncing now»
    signal the queue's status dot pulses on. Reads the existing lock so it
    adds no new state that could drift out of step with reality."""
    return _sync_lock.locked()


# --- Held mass-deletion state (proactive banner) -----------------------------
# Tabs whose bulk deletions the guard is currently HOLDING (looks like a bad
# read OR a real bulk delete — indistinguishable). Surfaced to the operator as
# a queue banner offering «Звірити видалення». In-memory process state (like the
# heartbeat); updated every tick per tab — set when held, cleared when the tab
# syncs clean or is force-reconciled. A restart re-derives it on the next tick.
_mass_vanish_lock = Lock()
_mass_vanish_pending: dict[str, int] = {}


def mass_vanish_pending() -> dict[str, int]:
    """Tabs → count of deletions currently held by the guard (for the banner)."""
    with _mass_vanish_lock:
        return dict(_mass_vanish_pending)


# Запобіжник для гілки «вкладку видалили цілком». Ті самі числа, що й у
# порядковому синку (app/sync.py): архівувати мовчки можна дрібницю, а масове
# зникнення — це майже завжди поганий листинг вкладок, а не робота людини.
_VANISHED_TAB_MIN_ORDERS = 5
_VANISHED_TAB_MAX_SHARE = 0.25

# Скільки листингів ПОСПІЛЬ мають не містити вкладку, перш ніж її роботи підуть
# в Архів. Разовий листинг без вкладки — не видалення, а звична поведінка
# проксі лабораторії (кешована/обрізана відповідь). Три тіки — три хвилини
# затримки для справжнього видалення старого дня і жодного шансу для разового
# збою переписати базу (08.09.26: один такий листинг забрав в Архів робочий
# день). Живе в памʼяті процесу, як і банер: після рестарту рахуємо заново.
_ABSENT_LISTINGS_BEFORE_ARCHIVE = 3
_absent_lock = Lock()
_absent_streak: dict[str, int] = {}


def _note_absent_listing(absent_tabs: set[str]) -> dict[str, int]:
    """Оновити лічильники «вкладки немає в листингу» і повернути їх зріз.

    Рахуються лише вкладки, відсутні САМЕ ЗАРАЗ і з активними роботами; усе,
    чого в `absent_tabs` нема, скидається — так «поспіль» означає поспіль.
    """
    with _absent_lock:
        for tab in list(_absent_streak):
            if tab not in absent_tabs:
                _absent_streak.pop(tab)
        for tab in absent_tabs:
            _absent_streak[tab] = _absent_streak.get(tab, 0) + 1
        return dict(_absent_streak)


def absent_tab_streaks() -> dict[str, int]:
    """Вкладки, яких зараз бракує в листингу → скільки читань поспіль."""
    with _absent_lock:
        return dict(_absent_streak)


def _reset_absent_streaks_for_tests() -> None:
    """Скинути ВЕСЬ процесний стан листингу: лічильники відсутності, попереджені
    дивні назви, останній листинг без сьогоднішньої вкладки."""
    global _missing_today_reported
    with _absent_lock:
        _absent_streak.clear()
    _odd_titles_warned.clear()
    _missing_today_reported = None


def _listing_is_trustworthy(
    all_dated_titles: set[str], today: date, newest_known: date | None = None
) -> bool:
    """Чи схожий список вкладок на справжній, а не на обрізану/кешовану відповідь.

    Проксі лабораторії вже ловили на застарілих відповідях. Листинг, у якому
    немає ані сьогоднішньої, ані вчорашньої вкладки, підозрілий: архівувати за
    ним не можна, бо вкладка поза вікном today±1 більше ніколи не
    перечитується, і день зник би з черги назавжди (аудит 05.09.26, синк H-3).

    Але «немає сьогоднішньої» саме по собі ще не доказ збою: CRM можна
    вимикати на дні, і в понеділок вранці найновіша вкладка законно з пʼятниці
    (памʼятка про догін простою). Тому дивимось не лише на календар, а й на
    НАЙНОВІШУ вкладку, яку ми вже бачили: листинг, що не дотягує навіть до
    неї, — це справді відповідь із минулого. Дотягує — віримо, навіть якщо
    сьогоднішньої вкладки ще ніхто не створив.
    """
    dates = {d for d in (_parse_tab_date(t) for t in all_dated_titles) if d is not None}
    if not dates:
        return False
    newest_listed = max(dates)
    if newest_listed >= today - timedelta(days=1):
        return True
    if newest_known is None:
        return False
    return newest_listed >= newest_known


# Вкладки, чию структуру ми НЕ впізнали, і чому. Тримаємо в памʼяті поруч із
# `_mass_vanish_pending` і показуємо тим самим банером: обидва стани про одне —
# «синк свідомо НЕ чіпає ці дані, поки людина не гляне».
_header_mismatch: dict[str, list[str]] = {}


def header_mismatch_pending() -> dict[str, list[str]]:
    """Вкладки зі зсунутими заголовками → перелік розбіжностей (для банера)."""
    with _mass_vanish_lock:
        return {tab: list(problems) for tab, problems in _header_mismatch.items()}


def _record_header_mismatch(tab: str, problems: list[str]) -> None:
    with _mass_vanish_lock:
        if problems:
            _header_mismatch[tab] = list(problems)
        else:
            _header_mismatch.pop(tab, None)


def _forget_header_mismatch_for_absent_tabs(existing_titles: set[str]) -> None:
    """Забути скарги на вкладки, яких у таблиці вже немає.

    Запис знімався лише при УСПІШНОМУ перечитуванні тієї самої вкладки. Якщо
    її потім перейменували або прибрали, скарга лишалась у памʼяті процесу
    назавжди — банер показував вкладку, якої не існує, і сховати його можна
    було тільки перезапуском (ревʼю 07.09.26, LOW).
    """
    if not existing_titles:
        return  # порожній листинг — не доказ, що вкладки зникли
    with _mass_vanish_lock:
        for tab in [t for t in _header_mismatch if t not in existing_titles]:
            _header_mismatch.pop(tab, None)


def _record_mass_vanish(tab: str, held: int) -> None:
    with _mass_vanish_lock:
        if held > 0:
            _mass_vanish_pending[tab] = held
        else:
            _mass_vanish_pending.pop(tab, None)


# --- Background full-history import -------------------------------------------
# «Імпортувати всю історію» is a minutes-long proxy read (one call per dated
# tab). Running it inline blocked the request thread with zero feedback — the
# tab just hung «loading» and the operator couldn't tell if it was alive. It
# now runs in a daemon thread with its own session; the request returns at
# once and the queue's status dot pulses «синхронізує…» until it finishes,
# then a one-shot flash surfaces the result as a toast on the next poll.
_import_state_lock = Lock()
_import_running = False
_import_flash: dict | None = None


def import_running() -> bool:
    with _import_state_lock:
        return _import_running


def pop_import_flash() -> dict | None:
    """Return and clear the one-shot completion notice for a background import.

    Popped by the queue's status poll, which turns it into a toast exactly
    once. Returns None when there is nothing new to announce."""
    global _import_flash
    with _import_state_lock:
        flash, _import_flash = _import_flash, None
        return flash


def start_background_import() -> bool:
    """Kick off a whole-sheet import in a daemon thread; return immediately.

    False when an import is already in flight (button pressed twice) so the
    caller can say «вже виконується» instead of starting a second run."""
    global _import_running
    with _import_state_lock:
        if _import_running:
            return False
        _import_running = True
    Thread(
        target=_run_background_import, name="sheet-import-history", daemon=True
    ).start()
    return True


_IMPORT_BUSY_RETRIES = 6
_IMPORT_BUSY_WAIT_SECONDS = 5.0


def _run_background_import() -> None:
    global _import_running, _import_flash
    try:
        # A manual sync waits only _MANUAL_LOCK_WAIT_SECONDS (10s) for the lock;
        # a periodic tick over a slow proxy can hold it longer, and this daemon
        # thread is not time-critical — so retry on «busy» instead of surfacing
        # a scary error right after telling the operator «почато».
        summary = None
        for attempt in range(_IMPORT_BUSY_RETRIES):
            try:
                with SessionLocal() as session:
                    summary = sync_google_sheets(
                        session, trigger="manual", full_history=True
                    )
                break
            except SheetSyncBusyError:
                if attempt == _IMPORT_BUSY_RETRIES - 1:
                    raise
                time.sleep(_IMPORT_BUSY_WAIT_SECONDS)
        flash = {
            "kind": "success",
            "message": "Історію таблиці імпортовано. " + summary_message(summary),
        }
    except SheetSyncBusyError:
        flash = {
            "kind": "info",
            "message": "Синхронізація зараз зайнята — імпорт історії не почався. "
            "Спробуйте ще раз за хвилину.",
        }
    except SheetSyncError as exc:
        flash = {"kind": "error", "message": str(exc)}
    except Exception:
        # Never let a background thread die with an unsurfaced traceback — the
        # operator is watching the dot, not the log. A generic, safe message
        # is better than a pulse that never settles.
        flash = {
            "kind": "error",
            "message": "Не вдалося імпортувати історію таблиці.",
        }
    finally:
        with _import_state_lock:
            _import_running = False
            _import_flash = flash


# Імена полів Order → колонки таблиці, як їх називає оператор. Без цього
# у звірці світилось би «material_color», а людина шукає «Колір роботи».
_FIELD_LABELS = {
    "work_order_no": "наряд",
    "job_code": "номер роботи",
    "quantity": "кількість",
    "material_color": "колір",
    "kind": "вид роботи",
    "due_time": "здати до",
    "technician_name": "технік",
    "cam_comment": "коментар",
    "sum3d_id": "Sum3D",
    "calculated_raw": "прорахував",
    "milled_raw": "відфрезерував",
    "last_milled_date": "дата фрезерування",
    "mill_count": "який раз",
    "client_name": "клієнт",
}


@dataclass
class SheetSyncSummary:
    tabs_processed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    rows_seen: int = 0
    tab_names: list[str] = field(default_factory=list)

    # ── Звірка «таблиця = база» (08.09.26) ──────────────────────────────
    # Оператор звіряв чергу з таблицею очима, бо система не мала способу
    # сказати «збігається». Ці лічильники знімаються ДО запису (див.
    # app/sync.SyncResult), тож вони кажуть саме те, що він перевіряв.
    compared_rows: int = 0
    agreed_rows: int = 0
    compared_fields: int = 0
    differed_fields: int = 0
    skipped_non_queue: int = 0
    # Яка колонка розходиться найчастіше. Коли розбіжність зʼявляється,
    # перше питання оператора — «а в чому саме», і відповідь у нас уже є:
    # порівняння й так іде по кожному полю окремо.
    differed_by_field: Counter = field(default_factory=Counter)
    # False, якщо бодай на одній вкладці цього проходу щось законно рухалось
    # (зсув рядків, ручне додавання, притримане масове зникнення). Тоді
    # розбіжність нормальна, і вердикт треба відкласти, а не бити на сполох.
    verdict_trustworthy: bool = True

    def agreement_line(self) -> str:
        """Один рядок для журналу й екрана — або чесне «не звірено».

        Ніколи не каже «розбіжностей 0», коли цього проходу щось рухалось:
        краще промовчати, ніж дати цифру, якій оператор потім не повірить.
        """
        if not self.compared_rows:
            return "звірка: нема що звіряти"
        rows = (
            f"{self.compared_rows} "
            + pluralize_uk(self.compared_rows, "рядок", "рядки", "рядків")
        )
        skipped = (
            f", пропущено {self.skipped_non_queue} нефрезерних"
            if self.skipped_non_queue
            else ""
        )
        if not self.verdict_trustworthy:
            return f"звірка відкладена: цього проходу рядки рухались ({rows} звірено{skipped})"
        differed = self.compared_rows - self.agreed_rows
        word = pluralize_uk(differed, "розбіжність", "розбіжності", "розбіжностей")
        where = ""
        if differed and self.differed_by_field:
            field_name, count = self.differed_by_field.most_common(1)[0]
            where = f", найчастіше {_FIELD_LABELS.get(field_name, field_name)} ({count})"
        return f"звірено {rows}, {word}: {differed}{where}{skipped}"


def _parse_tab_date(title: str) -> date | None:
    """Дата вкладки — тим самим правилом, що й усюди (`parse_sheet_tab`).

    Спільне джерело важливе саме тут: цей парсер вирішує, ЩО імпортувати і що
    вважати «зниклою вкладкою». Якби він тлумачив дату інакше за решту екранів,
    робота могла б жити на дні, якого черга не показує.
    """
    title = canonical_tab_title(title)
    if not _DATE_TAB_RE.fullmatch(title):
        return None
    return parse_sheet_tab(title)


_LAST_FULL_SYNC_KEY = "last_full_sync_date"
def _last_full_sync_date(session: Session) -> date | None:
    """Дата останнього успішного повного синку, або None.

    Зберігається як AppSetting, тому переживає рестарт і вимкнення застосунку
    — саме той стан, заради якого існує: вимкнули CRM, у таблиці накопичились
    дні, увімкнули — і ця дата каже, наскільки назад читати."""
    raw = get_setting(session, _LAST_FULL_SYNC_KEY)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _mark_full_sync(session: Session, today: date) -> None:
    set_setting(session, _LAST_FULL_SYNC_KEY, today.isoformat())


def _worksheets_to_sync(
    session: Session,
    spreadsheet,
    today: date,
    include_tabs: set[str] | None = None,
    full_history: bool = False,
) -> tuple[list, set[str]]:
    """Choose a bounded initial history, then the operational three-day window.

    Returns ``(worksheets_to_import, all_dated_tab_titles)`` — the second set
    is EVERY dated tab currently present in the document (imported or not),
    used by sync_google_sheets to delete orders orphaned by a whole tab being
    removed from the sheet.

    ``include_tabs`` force-adds specific tab titles (dd.mm.yy) even when they
    fall outside that window — used so a manual sync of a day the operator is
    actually looking at re-reads that older tab and can reconcile deletions
    there (a periodic run never revisits old tabs, for proxy-speed reasons).

    ``full_history`` imports EVERY dated tab in the document regardless of the
    window — a one-off "pull the whole sheet" the operator triggers explicitly
    (Settings → «Імпортувати всю історію»). It is intentionally NOT the periodic
    path: a background tick still only touches today±1 for proxy speed, and the
    reconciliation below never deletes already-imported old tabs (their titles
    stay in all_dated_titles), so history imported once persists cheaply."""
    include_tabs = include_tabs or set()
    has_sheet_orders = session.scalar(
        select(func.count(Order.id)).where(Order.source == "lab")
    ) > 0
    if has_sheet_orders:
        # Звичайна поведінка — сьогодні±1. Але якщо CRM була вимкнена кілька
        # днів, поки лабораторія працювала в таблиці, вікно «вчора» лишило б
        # роботи з пропущених днів поза чергою (фоновий синк старі вкладки не
        # перечитує заради швидкості проксі). Тому початок вікна відсувається
        # НАЗАД до дня останнього успішного повного синку — рівно на дні
        # простою, не глибше initial-вікна (щоб застарілий штамп не тягнув
        # пів року). Так «увімкнув після кількох днів» саме підтягує пропущене.
        first_day = today - timedelta(days=1)
        last_full = _last_full_sync_date(session)
        if last_full is not None and last_full < first_day:
            first_day = max(last_full, today - timedelta(days=_INITIAL_LOOKBACK_DAYS))
    else:
        first_day = today - timedelta(days=_INITIAL_LOOKBACK_DAYS)
    last_day = today + timedelta(days=1)

    dated = []
    all_dated_titles: set[str] = set()
    raw_titles: list[str] = []
    for worksheet in call_with_retry(spreadsheet.worksheets):
        raw_titles.append(worksheet.title)
        tab_date = _parse_tab_date(worksheet.title)
        if tab_date is None:
            continue
        # Канонічна назва («08.09.26», а не « 08.09.26»): саме вона стає
        # `Order.sheet_tab` і ключем у всіх порівняннях.
        title = canonical_tab_title(worksheet.title)
        if title != worksheet.title:
            _warn_odd_tab_title(session, worksheet.title, title)
        all_dated_titles.add(title)
        if (
            full_history
            or (first_day <= tab_date <= last_day)
            or title in include_tabs
        ):
            dated.append((tab_date, title, worksheet))
    dated.sort(key=lambda item: (item[0], item[1]))
    _report_missing_today(session, today, all_dated_titles, raw_titles)
    return [item[2] for item in dated], all_dated_titles


# Назви вкладок, про які вже попереджено в цьому процесі, і останній листинг
# без сьогоднішньої вкладки — щоб журнал не повторював одне й те саме щохвилини.
_odd_titles_warned: set[str] = set()
_missing_today_reported: tuple[str, ...] | None = None


def _warn_odd_tab_title(session: Session, raw: str, canonical: str) -> None:
    """Вкладка з «майже датою» в назві — один запис у журнал на процес."""
    if raw in _odd_titles_warned:
        return
    _odd_titles_warned.add(raw)
    logger.warning("Синк: вкладку %r читаю як %s — у назві зайві символи", raw, canonical)
    session.add(
        SyncLog(
            direction="sheet_to_db",
            sheet_tab=canonical,
            status="skipped",
            message=(
                f"назва вкладки {raw!r} містить зайві пробіли — читаю її як "
                f"{canonical}; краще перейменувати в таблиці"
            ),
        )
    )


def _report_unimported_rows(session: Session, tab: str, raw: list[list[str]]) -> None:
    """Рядок з Sum3D ID, який не став роботою, — це втрачена робота. Сказати.

    Мовчазна втрата рядка помітна лише тому, хто рахує руками: у таблиці 102
    одиниці, у CRM 96 (08.09.26). Тепер вона лишає слід із номерами рядків, і
    на неї можна дивитись, а не здогадуватись. Через глушник: доки рядок
    висить у вкладці, кожен тік писав би те саме.
    """
    lost = unimported_work_rows(raw)
    if not lost:
        log_throttle.clear(f"sync.unimported:{tab}")
        return
    numbers = ", ".join(str(row.row_number + HEADER_ROWS) for row in lost[:10])
    if log_throttle.due(f"sync.unimported:{tab}:{numbers}") is None:
        return
    logger.warning(
        "Синк %s: %d рядків із Sum3D не стали роботою (рядки таблиці %s)",
        tab, len(lost), numbers,
    )
    session.add(
        SyncLog(
            direction="sheet_to_db",
            sheet_tab=tab,
            status="skipped",
            message=(
                f"{len(lost)} рядків із Sum3D ID не потрапили в CRM "
                f"(рядки таблиці {numbers}) — бракує матеріалу, кількості або "
                "імені; допишіть у таблиці, і робота зайде"
            ),
        )
    )


def _report_missing_today(
    session: Session, today: date, dated_titles: set[str], raw_titles: list[str]
) -> None:
    """Сьогоднішньої вкладки немає серед датованих — сказати про це ВГОЛОС.

    08.09.26 день стояв «Сьогодні 0» без жодного сліду: синк мовчки не читав
    вкладку, яку не впізнав. Тепер журнал показує сам листинг (через %r —
    невидимий символ у назві видно лише так). Один запис на кожен НОВИЙ
    листинг, не щохвилини: лабораторія законно створює вкладку пізніше.
    """
    global _missing_today_reported
    if not raw_titles or tab_name_for(today) in dated_titles:
        _missing_today_reported = None
        return
    snapshot = tuple(raw_titles)
    if snapshot == _missing_today_reported:
        return
    _missing_today_reported = snapshot
    logger.warning(
        "Синк: вкладки за сьогодні (%s) немає серед датованих; листинг: %r",
        tab_name_for(today), raw_titles,
    )
    session.add(
        SyncLog(
            direction="sheet_to_db",
            sheet_tab=tab_name_for(today),
            status="skipped",
            message=(
                f"вкладки за сьогодні ({tab_name_for(today)}) немає серед датованих; "
                f"у таблиці є: {raw_titles!r}"
            ),
        )
    )


def _configuration(session: Session) -> tuple[str, str]:
    sheet_id = (get_google_sheet_id(session) or "").strip()
    credentials_json = (get_google_service_account_json(session) or "").strip()
    if not sheet_id:
        raise SheetSyncConfigurationError(
            "У налаштуваннях не вказано Google Sheet ID."
        )
    if not credentials_json:
        raise SheetSyncConfigurationError(
            "У налаштуваннях не додано JSON сервісного акаунта Google."
        )
    try:
        credentials = json.loads(credentials_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SheetSyncConfigurationError(
            "JSON сервісного акаунта Google має некоректний формат."
        ) from exc
    if not isinstance(credentials, dict):
        raise SheetSyncConfigurationError(
            "JSON сервісного акаунта Google має некоректний формат."
        )
    return sheet_id, credentials_json


def _safe_failure(exc: Exception) -> SheetSyncError:
    if isinstance(exc, SheetSyncError):
        return exc
    return SheetSyncError(
        "Не вдалося синхронізувати Google Таблицю. Перевірте доступ сервісного "
        "акаунта, Sheet ID та підключення до інтернету."
    )


def _record_failure(
    session: Session, tab_name: str | None, error: SheetSyncError, *, persist: bool
) -> None:
    session.rollback()
    if not persist:
        return
    session.add(
        SyncLog(
            direction="sheet_to_db",
            sheet_tab=tab_name,
            status="error",
            # Only our controlled, user-safe message is persisted. Credential
            # contents and raw third-party exception text never enter SyncLog.
            message=str(error),
        )
    )
    try:
        session.commit()
    except Exception:
        session.rollback()


def sync_google_sheets(
    session: Session,
    *,
    trigger: str = "manual",
    include_tabs: set[str] | None = None,
    full_history: bool = False,
    force_reconcile_tabs: set[str] | None = None,
) -> SheetSyncSummary:
    """Import relevant dated tabs tab-by-tab and persist an audit log.

    The first import covers the most recent 30 days plus tomorrow. Later runs
    cover yesterday, today and tomorrow.

    Each tab is committed on its own: a failure part-way through a multi-tab
    import (say tab 25 of 30) rolls back ONLY the failing tab and preserves the
    tabs already imported, instead of discarding the whole run. Retries
    (call_with_retry) absorb transient blips first, so a failure that still
    reaches here is treated as fatal for this run: the failing tab is rolled
    back, its sanitized error is logged, and processing stops rather than
    hammering an unhealthy connection through the remaining tabs. A setup
    failure (missing config, can't open/list the spreadsheet) imports nothing.

    The process-wide non-blocking lock prevents a background run and a button
    click (or two overlapping background ticks) from importing the same tab
    concurrently — same pattern as app/mail_sync_service.py. ``trigger`` is
    audit metadata (``manual`` or ``background``): background runs that find
    nothing new skip the SyncLog write entirely, and background failures are
    not persisted (only logged), so a prolonged outage doesn't fill the audit
    table with a row every couple of minutes.

    ``force_reconcile_tabs`` — вкладки (dd.mm.yy), для яких оператор СВІДОМО
    підтвердив масове видалення («Звірити видалення»): лише на них знімається
    поріг захисту від масової архівації. Решта вкладок цього ж прогону лишаються
    під захистом.
    """
    # Пауза зупиняє і ЧИТАННЯ, не лише запис: див. SheetSyncPausedError.
    # Ручний виклик має сказати про це вголос, фоновий — просто нічого не
    # робити (інакше кожні кілька хвилин у журнал сипався б однаковий рядок).
    if sync_control.is_paused():
        if trigger == "manual":
            raise SheetSyncPausedError(
                "Синхронізацію призупинено. Зніміть паузу, щоб синхронізувати."
            )
        return SheetSyncSummary()

    if trigger not in {"manual", "background"}:
        raise ValueError("unsupported sheet sync trigger")
    # A manual click waits out a hot-tab tick (~3s every 15s would otherwise
    # give the button a ~20% chance of a spurious "вже виконується"); the
    # background full sync stays non-blocking — its worker thread never
    # overlaps the hot lane anyway, so a busy lock there means a manual run
    # is in flight and this tick can just skip.
    if trigger == "manual":
        acquired = _sync_lock.acquire(timeout=_MANUAL_LOCK_WAIT_SECONDS)
    else:
        acquired = _sync_lock.acquire(blocking=False)
    if not acquired:
        raise SheetSyncBusyError(
            "Синхронізація Google Таблиці вже виконується. Спробуйте трохи пізніше."
        )

    forced_tabs = set(force_reconcile_tabs or ())
    summary = SheetSyncSummary()
    try:
        try:
            _configuration(session)
            spreadsheet = open_spreadsheet(db=session)  # retries internally
            # Always re-read tabs whose deletions the guard is currently HOLDING
            # (banner pending): a transient bad read then self-clears on the next
            # clean tick even if the tab has aged out of the normal window, and
            # «Звірити видалення» targets exactly these tabs. Without this a held
            # tab that leaves today±1 would keep its banner forever, and the
            # button (today±1) could never reach it.
            effective_include = set(include_tabs or ()) | set(mass_vanish_pending().keys())
            worksheets, all_dated_titles = _worksheets_to_sync(
                session, spreadsheet, business_today(),
                effective_include or None, full_history=full_history,
            )
            _forget_header_mismatch_for_absent_tabs(all_dated_titles)
        except Exception as exc:
            # Setup failure: nothing has been imported, so there is no partial
            # progress to preserve — surface it and record it like before.
            # У журнал користувача йде САНІТИЗОВАНИЙ текст (у винятках Google
            # трапляються ключі й токени), тож справжню причину зі стеком
            # лишаємо в лозі — інакше діагностувати збій нема по чому.
            logger.exception("Синк таблиці: збій підготовки")
            safe_error = _safe_failure(exc)
            _record_failure(session, None, safe_error, persist=trigger == "manual")
            raise safe_error from exc

        for worksheet in worksheets:
            current_tab = canonical_tab_title(worksheet.title)
            try:
                # Момент ЗНІМКА рядків — ДО виклику, не після. Рядка, якого в
                # цьому знімку немає, у таблиці справді немає; а робота,
                # створена ПІСЛЯ цієї мітки, могла дописати свій рядок уже
                # після читання, і її відсутність нічого не доводить. Саме ця
                # мітка (а не «дві хвилини від створення») тепер вирішує, кого
                # реконсиляція видалень не чіпає.
                read_at = utc_now()
                raw = call_with_retry(worksheet.get_all_values)
                # Структура вкладки — ПЕРЕД імпортом. Вставлена колонка зсуває
                # кожен індекс: «Ім'я техніка» читалось би як Sum3D, усі роботи
                # дня стали б «прийнято», частина зникла б із черги, а запис
                # Sum3D ліг би в чужу колонку (аудит 05.09.26, синк H-6).
                # Дешевша перевірка кількох якорів рятує і читання, і запис.
                problems = header_mismatches(raw)
                _record_header_mismatch(current_tab, problems)
                if problems:
                    session.add(
                        SyncLog(
                            direction="sheet_to_db",
                            sheet_tab=current_tab,
                            status="error",
                            message=(
                                "структура вкладки не впізнана, імпорт пропущено: "
                                + "; ".join(problems)
                            ),
                        )
                    )
                    session.commit()
                    continue
                rows = parse_rows(raw)
                _report_unimported_rows(session, current_tab, raw)
                # Read fill colours (best-effort) so client rows whose blue was
                # cleared flip to "видано" and grey SLM rows are filtered out.
                row_fills = fetch_row_fills(worksheet)
                result = sync_tab(
                    session, current_tab, rows,
                    row_fills=row_fills, raw_row_count=len(raw),
                    # A manual "Синхронізувати зараз" reconciles deletions
                    # immediately — the operator's deliberate click isn't the
                    # background poll the read/write grace guards against.
                    deletion_grace_seconds=0 if trigger == "manual" else 120,
                    rows_read_at=read_at,
                    # Оператор підтвердив масове видалення («звірити видалення»)
                    # — обходить поріг захисту від масової архівації, але ЛИШЕ
                    # на тих вкладках, які він підтвердив. Раніше прапорець був
                    # булевим і знімав запобіжник на ВСІХ вкладках прогону: одна
                    # підтверджена вкладка залишала без захисту сусідній день,
                    # прочитаний обрізано (аудит 05.09.26, синк H-4).
                    force_reconcile=current_tab in forced_tabs,
                )
                # Commit this tab before touching the next, so a later tab's
                # failure can never undo it.
                session.commit()
                # Update the proactive banner state for this tab (set when the
                # guard held a bulk deletion, cleared when it synced clean).
                _record_mass_vanish(current_tab, result.held_mass_vanish)
                if result.held_mass_vanish:
                    # Банер живе в памʼяті процесу й зникає з рестартом. Слід у
                    # журналі лишається: «того дня синк притримав N видалень» —
                    # єдине, за чим потім можна відновити, що саме сталось.
                    session.add(SyncLog(
                        direction="sheet_to_db", sheet_tab=current_tab, status="skipped",
                        message=(
                            f"притримано масове зникнення: {result.held_mass_vanish} "
                            "робіт не заархівовано — схоже на погане читання"
                        ),
                    ))
                    session.commit()
            except Exception as exc:
                logger.exception("Синк таблиці: збій на вкладці %s", current_tab)
                safe_error = _safe_failure(exc)
                _record_failure(session, current_tab, safe_error, persist=trigger == "manual")
                raise safe_error from exc

            summary.tabs_processed += 1
            summary.tab_names.append(current_tab)
            summary.rows_seen += len(rows)
            summary.created += result.created
            summary.updated += result.updated
            summary.unchanged += result.unchanged
            summary.deleted += result.deleted
            summary.compared_rows += result.compared_rows
            summary.agreed_rows += result.agreed_rows
            summary.compared_fields += result.compared_fields
            summary.differed_fields += result.differed_fields
            summary.skipped_non_queue += result.skipped_non_queue
            summary.differed_by_field.update(result.differed_by_field)
            if not result.verdict_is_trustworthy():
                summary.verdict_trustworthy = False

        # Orders orphaned by a WHOLE tab deleted from the sheet: the per-tab
        # reconciliation above only sees rows inside tabs that still exist, so
        # an order whose dated tab vanished would linger forever (and keep its
        # phantom day in the queue's day-strip). Deleting a tab in the sheet
        # means "this day's records are gone" — mirror that here for
        # sheet-sourced orders only; email orders are stamped with a business
        # date, not a real tab, and are never touched.
        # Найновіша вкладка, яку ми вже імпортували: нею звіряємо, чи листинг
        # не «з минулого» (див. _listing_is_trustworthy).
        newest_known = max(
            (
                d
                for d in (
                    _parse_tab_date(t)
                    for t in session.scalars(
                        select(Order.sheet_tab).where(
                            Order.source.in_(("lab", "sheet_client")),
                            Order.sheet_tab.isnot(None),
                        ).distinct()
                    )
                )
                if d is not None
            ),
            default=None,
        )
        if all_dated_titles and _listing_is_trustworthy(
            all_dated_titles, business_today(), newest_known
        ):
            orphans = [
                o
                for o in session.scalars(
                    select(Order).where(
                        Order.source.in_(("lab", "sheet_client")),
                        Order.sheet_tab.isnot(None),
                        Order.sheet_tab.notin_(all_dated_titles),
                        # Only ones still active — an already-archived order must
                        # not be re-stamped (and re-logged) on every full sync.
                        Order.archived_at.is_(None),
                    )
                )
                # Only orders whose sheet_tab actually names a dated tab: a
                # non-dd.mm.yy value was never a real sheet tab, so its absence
                # from the listing proves nothing.
                if _parse_tab_date(o.sheet_tab) is not None
            ]
            confirmed: set[str] = set()
            if orphans:
                # Той самий запобіжник, що й у порядкового синку (sync.py:811),
                # якого ця гілка не мала зовсім: неповний листинг вкладок (проксі
                # віддав кешовану чи обрізану відповідь) виглядає точно як
                # «вкладки видалили» — і цілі робочі дні йшли в Архів, звідки
                # самі не повертаються, бо фонове вікно їх більше не читає
                # (аудит 05.09.26, синк H-3).
                active_total = session.scalar(
                    select(func.count()).select_from(Order).where(
                        Order.source.in_(("lab", "sheet_client")),
                        Order.sheet_tab.isnot(None),
                        Order.archived_at.is_(None),
                    )
                ) or 0
                orphans_by_tab: dict[str, int] = {}
                for o in orphans:
                    orphans_by_tab[o.sheet_tab] = orphans_by_tab.get(o.sheet_tab, 0) + 1
                gone_tabs = sorted(orphans_by_tab)
                confirmed = {tab for tab in gone_tabs if tab in forced_tabs}
                # Частка від УСІХ активних робіт не ловить зникнення ОДНОГО
                # дня: черга тримає 30 днів, день ≈ 10 % від неї, а поріг 25 %.
                # 08.09.26 листинг без сьогоднішньої вкладки (але з наперед
                # створеними 09.09–14.09) пройшов перевірку довіри вище — і
                # ~100 робіт дня пішли в Архів за один тік як «дрібниця».
                # Вкладку робочого вікна (вчора/сьогодні/завтра) ніхто не
                # видаляє свідомо, тож її зникнення з більш ніж
                # _VANISHED_TAB_MIN_ORDERS роботами — завжди «тримати й
                # питати», незалежно від частки.
                work_today = business_today()
                window_lo = work_today - timedelta(days=1)
                window_hi = work_today + timedelta(days=1)
                window_tab_gone = any(
                    count > _VANISHED_TAB_MIN_ORDERS
                    and (tab_date := _parse_tab_date(tab)) is not None
                    and window_lo <= tab_date <= window_hi
                    for tab, count in orphans_by_tab.items()
                    if tab not in confirmed
                )
                mass = window_tab_gone or (
                    len(orphans) > _VANISHED_TAB_MIN_ORDERS
                    and len(orphans) > _VANISHED_TAB_MAX_SHARE * active_total
                )
                if mass and not confirmed:
                    # Тримаємо: пишемо слід, піднімаємо банер по кожній вкладці
                    # і НЕ архівуємо. Наступний чистий тік зніме це сам, а
                    # «Звірити видалення» дає операторові підтвердити свідомо.
                    session.add(
                        SyncLog(
                            direction="sheet_to_db",
                            status="error",
                            message=(
                                f"притримано архівацію {len(orphans)} робіт зі зниклих "
                                f"вкладок ({', '.join(gone_tabs)}): "
                                + (
                                    "вкладка робочого дня (вчора/сьогодні/завтра) "
                                    "сама не зникає"
                                    if window_tab_gone
                                    else f"це понад {int(_VANISHED_TAB_MAX_SHARE * 100)}% "
                                    "активних робіт"
                                )
                                + " — схоже на неповний листинг вкладок, а не на видалення"
                            ),
                        )
                    )
                    for tab in gone_tabs:
                        _record_mass_vanish(tab, sum(1 for o in orphans if o.sheet_tab == tab))
                    orphans = []
                elif mass:
                    # Підтверджено — архівуємо ЛИШЕ підтверджені вкладки.
                    orphans = [o for o in orphans if o.sheet_tab in confirmed]
                    gone_tabs = sorted(confirmed)
            # ОДИН листинг без вкладки — ще не видалення. Проксі лабораторії
            # віддає кешовані/обрізані відповіді; разова така відповідь не має
            # права переписати базу. Вкладка мусить бути відсутня в
            # _ABSENT_LISTINGS_BEFORE_ARCHIVE читаннях ПОСПІЛЬ (лічильник
            # скидається, щойно вона зʼявилась). Підтверджені оператором
            # («Звірити видалення») ідуть одразу — це його свідоме рішення.
            pending_tabs = {o.sheet_tab for o in orphans} - confirmed
            streaks = _note_absent_listing(pending_tabs)
            waiting = {
                tab for tab in pending_tabs
                if streaks.get(tab, 0) < _ABSENT_LISTINGS_BEFORE_ARCHIVE
            }
            if waiting:
                for tab in sorted(waiting):
                    if streaks.get(tab) != 1:
                        continue
                    count = sum(1 for o in orphans if o.sheet_tab == tab)
                    # У файловий лог — САМ листинг через %r: невидимий пробіл
                    # у назві вкладки видно лише так.
                    logger.warning(
                        "Синк: вкладки %r немає в листингу (%d активних робіт); "
                        "листинг: %r",
                        tab, count, sorted(all_dated_titles),
                    )
                    session.add(
                        SyncLog(
                            direction="sheet_to_db",
                            sheet_tab=tab,
                            status="skipped",
                            message=(
                                f"вкладка зникла з листингу ({count} робіт) — в Архів "
                                f"лише після {_ABSENT_LISTINGS_BEFORE_ARCHIVE} читань "
                                "поспіль без неї"
                            ),
                        )
                    )
                orphans = [o for o in orphans if o.sheet_tab not in waiting]
            if orphans:
                # Слід у журналі — ПЕРЕД архівацією: якщо коміт не дійде, у
                # SyncLog все одно лишиться, які саме дні зникли з листингу.
                gone_tabs = sorted({o.sheet_tab for o in orphans})
                session.add(
                    SyncLog(
                        direction="sheet_to_db",
                        status="ok",
                        message=(
                            f"архівовано {len(orphans)} робіт зі зниклих вкладок: "
                            + ", ".join(gone_tabs)
                        ),
                    )
                )
                # Keep, don't delete: a whole tab removed from the sheet (the lab
                # prunes old days for space) archives its orders instead of wiping
                # them — they leave the working queue but stay findable in the
                # Archive. Email orders and non-dated sheet_tab values are untouched.
                archived_at = utc_now()
                for orphan in orphans:
                    orphan.archived_at = archived_at
                    summary.deleted += 1
                for tab in gone_tabs:
                    _record_mass_vanish(tab, 0)
                # Лічильник відсутності виконав своє — не висить після архівації.
                with _absent_lock:
                    for tab in gone_tabs:
                        _absent_streak.pop(tab, None)

        # Результат звірки — у памʼять процесу, щоб плита в Налаштуваннях
        # показувала ОСТАННЮ звірку, а не рахувала її заново на кожен рендер.
        if summary.compared_rows:
            record_agreement(
                summary.agreement_line(),
                rows=summary.compared_rows,
                differed=summary.compared_rows - summary.agreed_rows,
                trustworthy=summary.verdict_trustworthy,
            )

        if trigger == "manual" or summary.created or summary.updated or summary.deleted:
            session.add(
                SyncLog(
                    direction="sheet_to_db",
                    status="ok",
                    message=(
                        f"trigger {trigger}; tabs {summary.tabs_processed}; "
                        f"rows {summary.rows_seen}; created {summary.created}; "
                        f"updated {summary.updated}; unchanged {summary.unchanged}; "
                        f"deleted {summary.deleted}; {summary.agreement_line()}"
                    ),
                )
            )
        # Позначаємо цей день як синхронізований УСПІШНО — доходимо сюди лише
        # коли всі вкладки вікна імпортовано без фатальної помилки. Звідси
        # рахується «скільки днів простою» при наступному ввімкненні.
        # Робоча дата, як і решта «сьогодні»: інакше о 00:30 штамп стрибав би
        # на наступний день і догін простою рахувався б від чужої дати.
        # Прогін, який не прочитав ЖОДНОЇ вкладки, днем не вважається: інакше
        # один порожній листинг (проксі віддав нічого) закривав би догін
        # простою, і пропущені дні більше не перечитались би ніколи.
        if summary.tabs_processed:
            _mark_full_sync(session, business_today())
        session.commit()
        return summary
    finally:
        _sync_lock.release()


def sync_sheets_background(session: Session) -> SheetSyncSummary:
    """Background-job entry point sharing the same locking and audit path."""
    return sync_google_sheets(session, trigger="background")


def sync_hot_tab(
    session: Session,
    *,
    today: date | None = None,
    extra_days: set[date] | None = None,
    neighbours: bool = True,
) -> SheetSyncSummary | None:
    """Fast lane: re-read only the operationally "hot" tabs — today's and
    yesterday's, plus ``extra_days`` (the days operators are viewing right now,
    tracked by the queue's poll — so "the open tab in the CRM" is always fast).
    Yesterday stays hot because the morning handout works out of yesterday's
    tab, and the user's "current day" is whichever tab the floor is actually
    in, not the calendar date.

    The full sync (worksheets listing + 3 tabs) costs tens of seconds through
    the lab proxy, but with the per-thread spreadsheet/worksheet cache warm
    (app/sheets.py) a single tab's `get_all_values` is ~3s — cheap enough to
    poll every ~15s so technician edits reach the CRM almost live (and a
    mistyped comment that gets corrected self-heals within one tick). Runs on
    the same background worker thread as the full sync, sharing its warm cache.

    Never raises on the busy lock — if a full/manual sync is in flight it
    already covers these tabs, so this tick just returns None ("skipped"). No
    SyncLog rows: this runs four times a minute and would flood the audit
    table; the full sync keeps owning audit logging. Each tab commits on its
    own (a failure on the second tab never rolls back the first). Returns a
    summary or None when skipped / no hot tab exists yet."""
    # Пауза — це «не чіпайте таблицю зараз». Гарячий тік лише прискорює те, що
    # повний синк зробить сам, тож на паузі він просто не ходить.
    if sync_control.is_paused():
        return None
    if not _sync_lock.acquire(blocking=False):
        return None
    # Гальмо квоти. «Турбо» (тік 5 с) × до 4 вкладок × 2 виклики ≈ 100 запитів
    # на хвилину при ліміті Google 60 — тобто систематичний 429, а не випадковий
    # (аудит 05.09.26, синк H-7). Коли лічильник підходить до краю, гарячий тік
    # ПРОПУСКАЄМО: він і так лише прискорює те, що повний синк зробить сам.
    # Робимо це ДО open_spreadsheet, бо саме він і є першим викликом.
    if quota_is_tight():
        _sync_lock.release()
        logger.info(
            "Гарячий тік пропущено: %d запитів до Sheets за останню хвилину",
            api_calls_last_minute(),
        )
        return None
    try:
        _configuration(session)
        spreadsheet = open_spreadsheet(db=session)
        base_day = today or business_today()
        # `neighbours=False` — вузький тік: ТІЛЬКИ сьогоднішня вкладка.
        # Нова робота зʼявляється лише в ній, а вчора й переглянуті дні
        # потрібні видачі, де секунди нічого не вирішують. Один тік коштує
        # 2 запити замість 8, і саме на цій різниці «Турбо» перестає впиратись
        # у гальмо квоти (див. SYNC_SPEED_PRESETS, ключ "wide").
        hot_days = [base_day]
        if neighbours:
            hot_days.append(base_day - timedelta(days=1))
            for extra in sorted(extra_days or ()):
                if extra not in hot_days:
                    hot_days.append(extra)
        summary = SheetSyncSummary()
        for day in hot_days:
            tab_title = tab_name_for(day)
            worksheet = get_worksheet_by_name(spreadsheet, tab_title)
            if worksheet is None:
                continue  # tab not created yet (early morning) — skip
            read_at = utc_now()  # див. коментар у повному синку
            raw = call_with_retry(worksheet.get_all_values)
            # Та сама звірка структури, що й у повному синку: гаряча смуга
            # читає ті самі вкладки кожні 15 с, і без перевірки саме вона
            # першою розтягла б зсунуті колонки по базі.
            problems = header_mismatches(raw)
            _record_header_mismatch(tab_title, problems)
            if problems:
                continue
            rows = parse_rows(raw)
            # Colours are cheap now (the CF-bloat cleanup took the metadata
            # fetch from ~7s to ~0.3s), so the hot lane reads them too: blue
            # clears flip to "видано" and grey SLM rows are filtered within
            # one ~15s tick instead of waiting for the next full sync.
            row_fills = fetch_row_fills(worksheet)
            result = sync_tab(
                session, tab_title, rows, row_fills=row_fills, raw_row_count=len(raw),
                rows_read_at=read_at,
            )
            session.commit()
            summary.tabs_processed += 1
            summary.tab_names.append(tab_title)
            summary.rows_seen += len(rows)
            summary.created += result.created
            summary.updated += result.updated
            summary.unchanged += result.unchanged
            summary.deleted += result.deleted
            summary.compared_rows += result.compared_rows
            summary.agreed_rows += result.agreed_rows
            summary.compared_fields += result.compared_fields
            summary.differed_fields += result.differed_fields
            summary.skipped_non_queue += result.skipped_non_queue
            summary.differed_by_field.update(result.differed_by_field)
            if not result.verdict_is_trustworthy():
                summary.verdict_trustworthy = False
        if summary.tabs_processed == 0:
            return None
        return summary
    except Exception as exc:
        session.rollback()
        logger.exception("Гарячий тік синку впав")
        raise _safe_failure(exc) from exc
    finally:
        _sync_lock.release()


def summary_message(summary: "SheetSyncSummary") -> str:
    """Один рядок для оператора про результат синку — те, що показує тост.

    Живе поруч із самим підсумком: його показують і черга (ручний синк,
    імпорт історії), і налаштування, тож формулювання має бути одне."""
    if summary.tabs_processed == 0:
        return "Підключення працює, але в доступному періоді не знайдено датованих вкладок."
    message = (
        f"Синхронізовано вкладок: {summary.tabs_processed}. "
        f"Нових робіт: {summary.created}, оновлено: {summary.updated}, "
        f"без змін: {summary.unchanged}."
    )
    # Звірка йде ОДРАЗУ за підсумком: заради неї оператор і відкриває таблицю
    # поруч. Формулювання одне на журнал, тост і плиту в налаштуваннях.
    message += f" {summary.agreement_line().capitalize()}."
    if summary.deleted:
        message += f" Видалено (немає в таблиці): {summary.deleted}."
    return message
