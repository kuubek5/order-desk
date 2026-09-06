"""Запис у Google Таблицю: точкові правки, заливка рядків, відновлення.

Один довгоживучий воркер (`sheet_writeback_pool`) обслуговує всі записи —
саме він робить кеш відкритої таблиці корисним і серіалізує запис, щоб дві
швидкі правки однієї клітинки не лягли в іншому порядку.

Таблиця тут завжди best-effort: джерело правди — база. Помилка запису йде
в SyncLog і повертається рядком, а не летить винятком в обличчя операторові.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from datetime import date, datetime
import logging

from sqlalchemy.orm import Session

from app import sync_control
from app.business_day import business_today
from app.db import SessionLocal
from app.models import Comment, Order, SyncLog
from app.parser import HEADER_ROWS
from app.sheet_writer import (
    append_manual_work_rows,
    append_order_comment,
    clear_order_row,
    clear_row_fills,
    paint_row_fills,
    RowOccupiedError,
    resolve_order_row,
    restore_order_row,
    write_calculated,
    write_order_fields,
    write_rework_calculated,
    write_rework_sum3d,
)
from app.sheets import get_worksheet_by_name, latest_worksheet_on_or_before, open_spreadsheet

logger = logging.getLogger(__name__)

# Single long-lived worker for sheet write-backs. Using ONE reused thread (not a
# fresh Thread per edit) is what makes the cached spreadsheet/worksheet objects
# pay off: only the worker's first write eats the ~18s open + ~18s worksheet
# lookup on the lab PC's link; every write after that reuses the warm per-thread
# cache and costs just the ~3s batch_update. It also serialises writes, so two
# quick edits to the same cell can't land out of order.
sheet_writeback_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sheet-writeback")



# ── Пауза синку: остання лінія, спільна для ВСІХ записів ──────────────────
# Гейт `sync_control.is_paused()` стоїть у роутах — там він і має бути, бо
# видає операторові зрозумілу відповідь («синк на паузі»). Але роутів чотирнадцять,
# і один з них (коментар до роботи) гейта не мав узагалі: адмін ставив паузу,
# щоб перебудувати вкладку руками, а коментар усе одно летів у живу клітинку
# K посеред його правки (аудит 05.09.26, синк M-8).
#
# Тому пауза перевіряється ще й ТУТ, в одній точці, через яку проходить кожен
# запис у таблицю. Це не заміна гейтам у роутах, а страховка: роут відповідає
# за повідомлення, пул — за те, щоб під час паузи в Google не пішло НІЧОГО.
SHEET_PAUSED_MESSAGE = "синк на паузі — у таблицю не записано"


def _guard_paused(fn):
    """Обгортка задачі пулу: на паузі не викликає fn, а каже, що пропустила."""

    @wraps(fn)
    def guarded(*args, **kwargs):
        if sync_control.is_paused():
            logger.info("Запис у таблицю пропущено (пауза): %s", getattr(fn, "__name__", fn))
            return SHEET_PAUSED_MESSAGE
        return fn(*args, **kwargs)

    return guarded


def submit_sheet_write(fn, *args, **kwargs):
    """Єдина точка входу в пул write-back. Усі записи йдуть через неї, тож
    пауза й будь-яка майбутня спільна умова додаються в ОДНОМУ місці."""
    return sheet_writeback_pool.submit(_guard_paused(fn), *args, **kwargs)


def warm_sheet_writeback() -> None:
    """Прогріти кеш воркера (відкрити таблицю один раз), щоб перша ж правка
    оператора долетіла за ~3с, а не за ~40с. Best effort — мовчки пропускає,
    якщо таблиця не налаштована або недоступна."""
    try:
        with SessionLocal() as warm_db:
            ss = open_spreadsheet(db=warm_db)
            # Also cache today's worksheet on this thread: a manual client
            # add (create_manual_order) runs its append here, and the
            # worksheet() metadata fetch is another ~18s cold on the lab
            # proxy — warming it now keeps the add to just the append.
            # business_today(), не date.today(): о 02:00 нічний оператор веде ще
            # ВЧОРАШНІЙ день, і прогрівати треба його вкладку — інакше перша ж
            # правка вночі платить ~40 с холодного відкриття (аудит, синк LOW).
            get_worksheet_by_name(ss, business_today().strftime("%d.%m.%y"))
    except Exception:
        logger.info("Sheet write-back warmup skipped (sheet not ready)")


def write_sheet_fields(db: Session, order: Order, fields: set[str]) -> str | None:
    """Write explicit portal changes and record the outcome without hiding it.

    Being an actual sheet row is the real gate, not sheet_tab truthiness: IMAP
    "email" orders now also carry a sheet_tab-shaped business date (set at
    accept time, see accept_email) so they date-bucket/overdue exactly like
    table orders, but they were never a row in the shared spreadsheet and must
    never trigger a write there. Both "lab" work rows and "sheet_client" client
    rows ARE real sheet rows (matched back by row_number), so both write back.
    """
    if not fields or order.source not in ("lab", "sheet_client") or not order.sheet_tab:
        return None
    try:
        worksheet = get_worksheet_by_name(open_spreadsheet(db=db), order.sheet_tab)
        if worksheet is None:
            raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
        written = write_order_fields(worksheet, order, fields)
        if not written:
            # Пропуск ≠ успіх. Рядок не підтверджено (зсунувся неоднозначно або
            # перевірочне читання впало), тож ми свідомо НЕ писали — але доти
            # в журнал ішло `ok`, і оператор не бачив, що саме цей запис у
            # таблицю не дійшов (знахідка живого тесту 06.09.26, S.8). Дані в
            # базі правильні й чужий рядок цілий; бракувало сигналу.
            message = "рядок у таблиці не підтверджено — не записано, спробуйте ще раз"
            db.add(
                SyncLog(
                    direction="db_to_sheet",
                    sheet_tab=order.sheet_tab,
                    status="skipped",
                    message=f"order {order.id}: {', '.join(sorted(fields))}: {message}",
                )
            )
            return message
        db.add(
            SyncLog(
                direction="db_to_sheet",
                sheet_tab=order.sheet_tab,
                status="ok",
                message=f"order {order.id}: {', '.join(sorted(fields))}",
            )
        )
        return None
    except Exception as exc:
        error = str(exc)
        db.add(
            SyncLog(
                direction="db_to_sheet",
                sheet_tab=order.sheet_tab,
                status="error",
                message=f"order {order.id}: {error}",
            )
        )
        return error


def append_manual_rows_warm(
    target_date: date, works: list[dict], *, paint_blue: bool, placement: str,
    target_tab: str = "",
) -> tuple[str, list[int]] | None:
    """Append a batch of manual work rows on the write-back worker thread, whose
    per-thread spreadsheet/worksheet cache stays warm (see warm_sheet_writeback)
    — so this runs in seconds instead of the ~40s cold open the request thread
    would pay. Uses its own DB session for the settings/config read.

    ``target_tab`` (dd.mm.yy) is the day tab the operator has on screen and wins
    when that tab exists — adding a work while looking at tomorrow must land in
    tomorrow, not silently in today. Falling back on it also covers a tab the
    operator sees but that has since been renamed away.

    Otherwise writes to the newest dated tab on or before ``target_date``: the
    lab often works a day or two behind, so today's tab may not exist yet — the
    row goes into the last available day rather than failing. Returns
    (resolved_tab_title, 1-indexed sheet rows), or None if the document has no
    dated tab at all."""
    from time import perf_counter

    with SessionLocal() as s:
        t0 = perf_counter()
        spreadsheet = open_spreadsheet(db=s)
        t_open = perf_counter()
        worksheet = None
        if target_tab:
            worksheet = get_worksheet_by_name(spreadsheet, target_tab)
        if worksheet is None:
            worksheet = latest_worksheet_on_or_before(spreadsheet, target_date)
        t_tab = perf_counter()
        if worksheet is None:
            return None
        rows = append_manual_work_rows(
            worksheet, works, paint_blue=paint_blue, placement=placement,
        )
        t_write = perf_counter()
        # Розбивка фаз: без неї «додавання довге» неможливо діагностувати —
        # 40с холодного open і 3с запису лікуються по-різному.
        logger.info(
            "MANUAL-ADD timing: open=%.2fs tab=%.2fs write=%.2fs total=%.2fs rows=%d",
            t_open - t0, t_tab - t_open, t_write - t_tab, t_write - t0, len(works),
        )
        return worksheet.title, rows


# Скільки чекати на відповідь Google, перш ніж відпустити оператора. Запис із
# черги пулу від цього не зникає — просто перестаємо тримати запит.
_AWAIT_WRITE_TIMEOUT_SECONDS = 90


async def await_on_writeback(fn, *args) -> str | None:
    """Виконати запис у таблицю на воркері write-back і дочекатись результату,
    НЕ блокуючи event loop.

    До аудиту 05.09.26 роути `async def` викликали gspread прямо на event loop:
    FastAPI виконує `async def` без threadpool, тож холодне відкриття таблиці
    (~40 с на лаб-проксі) або сон `call_with_retry` після 429 (20/40/60 с)
    заморожували ВЕСЬ застосунок — другий оператор бачив зависання, полл черги
    стояв. Тут робота йде на єдиний теплий потік пулу (він же серіалізує
    записи), а `await` віддає керування циклу подій.

    Повертає рядок помилки або None — той самий контракт, що й у синхронних
    `write_*`, щоб роут показав чесне «записано / не вдалось».
    """
    future = submit_sheet_write(fn, *args)
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(future), _AWAIT_WRITE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # Потік не переривається — запис лишається в черзі й, найпевніше,
        # дійде. Кажемо саме це, а не «не вдалося».
        logger.warning("Sheet write-back is taking longer than %ss", _AWAIT_WRITE_TIMEOUT_SECONDS)
        return "таблиця не відповідає — запис лишився в черзі й може ще пройти"
    except Exception as exc:  # noqa: BLE001 — помилка йде в тост, не в 500
        logger.exception("Sheet write-back on the pool failed")
        return str(exc) or "запис у таблицю не вдався"


def write_sheet_fields_warm(order_id: int, fields: set[str]) -> str | None:
    """`write_sheet_fields` на воркері: власна сесія (сесії SQLAlchemy не
    потоко-безпечні), значення читаються з БД, тож викликач мусить спершу
    закомітити свої зміни."""
    with SessionLocal() as bg:
        order = bg.get(Order, order_id)
        if order is None:
            return None
        error = write_sheet_fields(bg, order, fields)
        bg.commit()
        return error


def write_calculated_cell_warm(order_id: int, value: str) -> str | None:
    """`write_calculated_cell` на воркері — див. `write_sheet_fields_warm`."""
    with SessionLocal() as bg:
        order = bg.get(Order, order_id)
        if order is None:
            return None
        error = write_calculated_cell(bg, order, value)
        bg.commit()
        return error


def write_rework_sum3d_fields_warm(
    order_id: int, value: str, letter: str | None = None
) -> str | None:
    """`write_rework_sum3d_fields` на воркері — див. `write_sheet_fields_warm`."""
    with SessionLocal() as bg:
        order = bg.get(Order, order_id)
        if order is None:
            return None
        error = write_rework_sum3d_fields(bg, order, value, letter)
        bg.commit()
        return error


def write_sheet_fields_background(order_id: int, fields: set[str]) -> None:
    """Queue a sheet write-back on the shared writer so the request returns
    immediately. The DB is already committed by the caller; this mirrors the
    change into the sheet with its own session and records the outcome in
    SyncLog. A lost write self-heals on the next point edit — the DB is the
    source of truth."""
    def worker() -> None:
        try:
            with SessionLocal() as bg:
                order = bg.get(Order, order_id)
                if order is not None:
                    write_sheet_fields(bg, order, fields)
                    bg.commit()
        except Exception:
            logger.exception("Background sheet write-back failed for order %s", order_id)

    submit_sheet_write(worker)


def append_comment_background(order_id: int, comment_id: int, line: str) -> None:
    """Дописати коментар у таблицю фоном, не тримаючи оператора.

    Раніше `add_order_comment` відкривав таблицю прямо в потоці запиту, і
    додавання коментаря зависало на час відповіді Google (на лаб-проксі
    ~3с теплим, до ~40с холодним). Коментар у базі — головне; запис у
    таблицю best-effort, як і решта write-back. Помилка йде в SyncLog, а не
    в обличчя операторові.

    Ставить `order.cam_comment` і `comment.synced_at` у власній сесії, тому
    запит комітить коментар одразу, а таблиця наздоганяє."""
    def worker() -> None:
        try:
            with SessionLocal() as bg:
                order = bg.get(Order, order_id)
                comment = bg.get(Comment, comment_id)
                if order is None or order.sheet_tab is None or order.source != "lab":
                    return
                try:
                    worksheet = get_worksheet_by_name(open_spreadsheet(db=bg), order.sheet_tab)
                    if worksheet is None:
                        raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
                    order.cam_comment = append_order_comment(worksheet, order, line)
                    if comment is not None:
                        comment.synced_at = datetime.now()
                    bg.add(SyncLog(
                        direction="db_to_sheet", sheet_tab=order.sheet_tab,
                        status="ok", message=f"order {order_id}: comment",
                    ))
                except Exception as exc:  # noqa: BLE001 — не валимо, лишаємо слід у SyncLog
                    bg.add(SyncLog(
                        direction="db_to_sheet", sheet_tab=order.sheet_tab,
                        status="error", message=f"order {order_id}: comment: {exc}",
                    ))
                bg.commit()
        except Exception:
            logger.exception("Background comment append failed for order %s", order_id)

    submit_sheet_write(worker)


def set_client_row_fill(db: Session, order: Order, *, blue: bool) -> str | None:
    """Paint one sheet_client row's A:K fill blue (pending) or white (issued/
    found) to mirror a handout status change in the shared sheet. No-op for
    orders that don't live in a sheet row. Returns an error string on failure
    (never raises — the local status change must stand regardless)."""
    if order.source != "sheet_client" or not order.sheet_tab or order.row_number is None:
        return None
    try:
        spreadsheet = open_spreadsheet(db=db)
        worksheet = get_worksheet_by_name(spreadsheet, order.sheet_tab)
        if worksheet is None:
            return None
        # Позицію звіряємо перед фарбуванням: після видалення рядка вище
        # збережений row_number показує на СУСІДНЮ живу роботу, і «видано»
        # проставилось би не тому клієнту (аудит 05.09.26, синк H-5).
        row = resolve_order_row(worksheet, order)
        if row is None:
            return "рядок у таблиці не підтверджено — заливку не змінено"
        rows = [(worksheet.id, row)]
        if blue:
            paint_row_fills(spreadsheet, rows)
        else:
            clear_row_fills(spreadsheet, rows)
    except Exception as exc:  # noqa: BLE001 — never fail the status change over this
        logger.exception("Failed to set fill for order %s (blue=%s)", order.id, blue)
        return str(exc)
    return None


def set_client_row_fill_background(order_id: int, *, blue: bool) -> None:
    """Перефарбувати рядок клієнта в таблиці, не тримаючи оператора.

    Раніше це робилось прямо в обробнику кліку — і не абиде, а на потоці
    ЗАПИТУ, повз теплий кеш write-back воркера. Тобто кожна галочка «знайдено»
    платила за відкриття таблиці заново: у бойовому логу такі відкриття
    коштували від секунд до 40+ (`MANUAL-ADD timing: open=...`). Оператор на
    видачі клацає галочки одну за одною, тож ця затримка діставалась йому
    десятки разів за ранок.

    Заливка — дзеркало стану, а не сам стан: джерело правди в базі, і втрачений
    мазок самолікується наступною точковою правкою (та сама логіка, що в
    write_sheet_fields_background)."""
    def worker() -> None:
        try:
            with SessionLocal() as bg:
                order = bg.get(Order, order_id)
                if order is None:
                    return
                error = set_client_row_fill(bg, order, blue=blue)
                if error:
                    logger.warning(
                        "Заливку рядка для роботи %s не оновлено: %s", order_id, error
                    )
                bg.commit()
        except Exception:
            logger.exception("Фонова заливка рядка не вдалася для роботи %s", order_id)

    submit_sheet_write(worker)


def clear_sheet_row_background(order_id: int) -> None:
    """Blank a deleted order's row in the sheet, on the write-back worker.

    BLANK, never delete: removing a row in Google shifts every row below it up,
    which would break the row_number linkage of all the works underneath (the
    exact corruption app/sync.py::_relink_moved_rows had to be written to
    repair). An all-empty row reads as free on the next sync, so nothing is
    re-imported and the neighbours keep their positions.

    Takes an order_id, not (tab, row): blanking wipes A:K, so the row must be
    confirmed to still hold THIS work. While the write sat in the pool queue a
    technician may have deleted a row above it, and the stored position would
    then point at someone else's live work (аудит 05.09.26, синк H-5).
    """
    def worker() -> None:
        try:
            with SessionLocal() as bg:
                order = bg.get(Order, order_id)
                if order is None or not order.sheet_tab or order.row_number is None:
                    return
                sheet_tab = order.sheet_tab
                worksheet = get_worksheet_by_name(open_spreadsheet(db=bg), sheet_tab)
                if worksheet is None:
                    logger.warning("Delete: sheet tab %s not found", sheet_tab)
                    return
                if not clear_order_row(worksheet, order):
                    bg.add(SyncLog(
                        direction="db_to_sheet", sheet_tab=sheet_tab, status="error",
                        message=(
                            f"order {order_id}: рядок не підтверджено — "
                            "стирання пропущено, приберіть рядок у таблиці вручну"
                        ),
                    ))
                    bg.commit()
                    return
                bg.add(
                    SyncLog(
                        direction="db_to_sheet", sheet_tab=sheet_tab, status="ok",
                        message=f"видалено роботу {order_id}: рядок очищено",
                    )
                )
                bg.commit()
        except Exception:
            logger.exception("Clearing sheet row failed for order %s", order_id)

    submit_sheet_write(worker)


def clear_group_fills_background(order_ids: list[int]) -> None:
    """Зняти синю заливку з рядків цілої групи — ОДНІЄЮ пакетною правкою.

    Це фонова пара до кнопки «Усі знайдено» (аудит 05.09.26, крок 3.5). Робити
    те саме циклом із `set_client_row_fill_background` не можна: у клієнта
    буває 50 робіт, і кожна поставила б у чергу воркера окреме завдання з
    власним пошуком рядка — та сама арифметика, що вже одного разу заморозила
    видачу на дві хвилини (синк C-2). Тут таблиця відкривається один раз.

    Заливка — дзеркало стану, а не сам стан: статуси вже закомічені, і
    втрачений мазок самолікується наступною точковою правкою, тож помилки
    лише логуються.
    """
    if not order_ids:
        return

    def worker() -> None:
        try:
            with SessionLocal() as bg:
                fill_rows: list[tuple[int, int]] = []
                spreadsheet = None
                for order_id in order_ids:
                    order = bg.get(Order, order_id)
                    if (
                        order is None
                        or order.source != "sheet_client"
                        or not order.sheet_tab
                        or order.row_number is None
                    ):
                        continue
                    if spreadsheet is None:
                        spreadsheet = open_spreadsheet(db=bg)
                    worksheet = get_worksheet_by_name(spreadsheet, order.sheet_tab)
                    if worksheet is None:
                        continue
                    # Позицію звіряємо перед тим, як білити: після видалення
                    # рядка вище збережений row_number показує на чужу живу
                    # роботу (синк H-5).
                    row = resolve_order_row(worksheet, order)
                    if row is None:
                        logger.warning(
                            "Заливку рядка для роботи %s не знято: рядок не підтверджено",
                            order_id,
                        )
                        continue
                    fill_rows.append((worksheet.id, row))
                if fill_rows and spreadsheet is not None:
                    clear_row_fills(spreadsheet, fill_rows)
                bg.commit()
        except Exception:
            logger.exception("Фонове зняття заливки групи не вдалося")

    submit_sheet_write(worker)


def issue_group_warm(field_map: dict[int, list[str]]) -> str | None:
    """Увесь запис однієї видачі клієнта — ОДНИМ завданням на воркері.

    Раніше `issue_handout_group` робив це на event loop і по одній роботі:
    холодне відкриття таблиці (~40 с) плюс ~4 виклики на кожну з N робіт —
    «Видати 8 з 8» давало понад дві хвилини повністю замороженого застосунку
    (аудит 05.09.26, синк C-2). Тут таблиця відкривається один раз на теплому
    потоці, а біла заливка всіх рядків клієнта йде однією пакетною правкою.

    `field_map`: id роботи → поля-маркери, які порахував роут (він же вже
    закомітив статуси). Повертає перший рядок помилки або None.
    """
    if not field_map:
        return None
    error: str | None = None
    with SessionLocal() as bg:
        fill_rows: list[tuple[int, int]] = []
        spreadsheet = None
        for order_id, fields in field_map.items():
            order = bg.get(Order, order_id)
            if order is None:
                continue
            error = write_sheet_fields(bg, order, set(fields)) or error
            if (
                order.source != "sheet_client"
                or not order.sheet_tab
                or order.row_number is None
            ):
                continue
            try:
                if spreadsheet is None:
                    spreadsheet = open_spreadsheet(db=bg)
                worksheet = get_worksheet_by_name(spreadsheet, order.sheet_tab)
                if worksheet is None:
                    continue
                # Позицію звіряємо перед тим, як білити: після видалення рядка
                # вище збережений row_number показує на чужу живу роботу, і
                # «видано» знялось би не з того клієнта (синк H-5).
                row = resolve_order_row(worksheet, order)
                if row is None:
                    error = error or (
                        f"робота {order_id}: рядок у таблиці не підтверджено — "
                        "заливку не знято"
                    )
                    continue
                fill_rows.append((worksheet.id, row))
            except Exception as exc:  # noqa: BLE001 — статус «видано» лишається
                logger.exception("Handout group fill lookup failed for order %s", order_id)
                error = error or str(exc)
        if fill_rows and spreadsheet is not None:
            try:
                clear_row_fills(spreadsheet, fill_rows)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to clear blue fill for a handout group")
                error = error or str(exc)
        bg.commit()
    return error


def write_rework_sum3d_fields(
    db: Session, order: Order, value: str, letter: str | None = None
) -> str | None:
    """Write the redo Sum3D ID to the sheet's column W and, when ``letter`` is
    given, the operator's initial to the rework "Прорахував" cell (column X) —
    both in one sheet open. Same lab-only gate, single-cell discipline and
    error-surfacing as write_sheet_fields."""
    if order.source != "lab" or not order.sheet_tab:
        return None
    try:
        worksheet = get_worksheet_by_name(open_spreadsheet(db=db), order.sheet_tab)
        if worksheet is None:
            raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
        written = write_rework_sum3d(worksheet, order, value)
        if letter is not None and written:
            written = write_rework_calculated(worksheet, order, letter)
        if not written:
            # Той самий контракт, що у write_sheet_fields: пропуск видно
            # окремо від успіху (S.8).
            message = "рядок у таблиці не підтверджено — не записано, спробуйте ще раз"
            db.add(
                SyncLog(
                    direction="db_to_sheet",
                    sheet_tab=order.sheet_tab,
                    status="skipped",
                    message=f"order {order.id}: rework sum3d_id: {message}",
                )
            )
            return message
        db.add(
            SyncLog(
                direction="db_to_sheet",
                sheet_tab=order.sheet_tab,
                status="ok",
                message=f"order {order.id}: rework sum3d_id",
            )
        )
        return None
    except Exception as exc:
        error = str(exc)
        db.add(
            SyncLog(
                direction="db_to_sheet",
                sheet_tab=order.sheet_tab,
                status="error",
                message=f"order {order.id}: rework sum3d_id: {error}",
            )
        )
        return error


def restore_sheet_row_warm(order_id: int) -> str | None:
    """Re-fill a deleted work's sheet row, ON THE WRITE-BACK WORKER — never call
    this directly from a request thread.

    Running here is not an optimisation, it is correctness: deleting a work
    queues clear_sheet_row_background on this same single-worker pool, and a
    cold spreadsheet open costs tens of seconds through the lab proxy. A restore
    that ran on the request thread could therefore land BEFORE the still-queued
    blank, which would then wipe the row it had just restored — leaving the work
    un-archived in the CRM with an empty sheet row, which the next sync reads as
    "vanished" and archives all over again. The pool serialises writes, so
    queueing behind the blank is what makes undo deterministic.

    Uses its own session (SQLAlchemy sessions are not thread-safe) and reads the
    order's values, which the caller has already committed. Returns an error
    string, or None on success; never raises."""
    with SessionLocal() as bg:
        order = bg.get(Order, order_id)
        if order is None:
            return "роботи більше немає"
        if order.source not in ("lab", "sheet_client") or not order.sheet_tab or order.row_number is None:
            return None  # never had a sheet row (email work) — nothing to restore
        try:
            spreadsheet = open_spreadsheet(db=bg)
            worksheet = get_worksheet_by_name(spreadsheet, order.sheet_tab)
            if worksheet is None:
                raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
            restore_order_row(worksheet, order)
            if order.source == "sheet_client" and order.status not in ("видано", "знайдено при видачі"):
                # The sync reads the blue fill as the pending/issued flag, so a
                # restore that skipped it would silently flip the work to «видано».
                paint_row_fills(spreadsheet, [(worksheet.id, order.row_number + HEADER_ROWS)])
            bg.add(SyncLog(
                direction="db_to_sheet", sheet_tab=order.sheet_tab, status="ok",
                message=f"order {order.id}: рядок відновлено",
            ))
            bg.commit()
            return None
        except RowOccupiedError as exc:
            bg.rollback()
            bg.add(SyncLog(
                direction="db_to_sheet", sheet_tab=order.sheet_tab, status="error",
                message=f"order {order.id}: {exc}",
            ))
            bg.commit()
            return f"{exc} — впишіть роботу в таблицю вручну"
        except Exception as exc:  # noqa: BLE001 — reported to the operator, never raised
            error = str(exc)
            bg.rollback()
            bg.add(SyncLog(
                direction="db_to_sheet", sheet_tab=order.sheet_tab, status="error",
                message=f"order {order.id}: restore: {error}",
            ))
            bg.commit()
            return error


def restore_sheet_row(order: Order) -> str | None:
    """Restore a deleted work's sheet row and WAIT for the result.

    Blocking is deliberate. Undo of a delete is rare and explicit, and its two
    halves must not diverge: the caller needs to know whether the row actually
    came back before it decides to un-archive the order. Same submit-and-wait
    shape the manual-add path already uses (append_manual_rows_warm)."""
    try:
        return submit_sheet_write(restore_sheet_row_warm, order.id).result(timeout=120)
    except Exception as exc:  # noqa: BLE001 — includes the wait timing out
        logger.exception("Restoring sheet row failed for order %s", order.id)
        return str(exc) or "таблиця не відповідає"


def write_calculated_cell(db: Session, order: Order, value: str) -> str | None:
    """Write the «Оператор» / «Прорахував» cell (column М) DIRECTLY — the explicit
    manual edit must overwrite whatever is there (write_order_fields would instead
    preserve a non-empty live marker cell and silently drop the edit). Same
    lab/client-row gate and error-surfacing as write_sheet_fields."""
    if order.source not in ("lab", "sheet_client") or not order.sheet_tab:
        return None
    try:
        worksheet = get_worksheet_by_name(open_spreadsheet(db=db), order.sheet_tab)
        if worksheet is None:
            raise RuntimeError(f"вкладку '{order.sheet_tab}' не знайдено")
        if not write_calculated(worksheet, order, value):
            # Row shifted and its identity is ambiguous (duplicate client name on
            # the tab), so the write was skipped. Must NOT report success: the
            # sheet is authoritative for column М, so the next sync would quietly
            # revert the value and the operator would never know why.
            raise RuntimeError("рядок у таблиці не знайдено однозначно — значення не записано")
        db.add(
            SyncLog(
                direction="db_to_sheet", sheet_tab=order.sheet_tab, status="ok",
                message=f"order {order.id}: calculated_raw (operator)",
            )
        )
        return None
    except Exception as exc:
        error = str(exc)
        db.add(
            SyncLog(
                direction="db_to_sheet", sheet_tab=order.sheet_tab, status="error",
                message=f"order {order.id}: calculated_raw: {error}",
            )
        )
        return error
