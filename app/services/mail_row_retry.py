"""Рядок-нотатка поштової роботи, що не ліг у таблицю, — дописати самим.

Бриф запобіжників мультипрорахунку, п.1 (власник 05.10.26: «повний автомат»).
Прийняття листа пише рядок-нотатку ПІСЛЯ коміту бази (`mail_accept.
_write_placeholder_row`) і при збої лише лишало слід у журналі: робота жила в
черзі без рядка, логісти її не бачили, і ніхто її не дописував.

Два різні збої, і обидва тепер закриті:

* **запис не дійшов** (мережа, квота, немає вкладки) — позначка
  `Order.sheet_row_pending` лишається, банер «не дійшло в таблицю» показує
  роботу одразу, а цей повтор дописує рядок;
* **запис дійшов, відповідь загубилась** — рядок у таблиці є, робота про нього
  не знає. Дописати ще раз = два рядки, тому повтор спершу шукає такий
  «нічий» рядок (той самий ключ identity, що в синку, і той самий Sum3D) і
  прив'язує його. Синк робить те саме зі свого боку
  (`sync._relink_moved_rows`): інакше він завів би з цього рядка дубль.

Чому не одразу: повтор чекає `PENDING_ROW_GRACE_SECONDS` від прийняття. За цей
час синк кілька разів перечитає вкладку й сам упізнає рядок із загубленою
відповіддю — повтор дописує лише тоді, коли рядка справді немає. Повтор іде
на пулі write-back (`submit_sheet_write`): пауза синку, один теплий потік,
жодного запису на event loop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from time import monotonic
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import sync_control
from app.models import EmailMessage, Order, SyncLog
from app.services.sheet_stuck_writes import note_write_failed, note_write_ok

logger = logging.getLogger(__name__)

# Від прийняття до першого повтору: синк за цей час перечитує вкладку кілька
# разів і встигає впізнати рядок, чия відповідь загубилась.
PENDING_ROW_GRACE_SECONDS = 180.0
# Ту саму роботу повторюємо не частіше — як Sum3D і заливку.
PENDING_ROW_RETRY_SECONDS = 120.0
PENDING_ROW_RETRY_BATCH = 3
_attempts: dict[int, float] = {}


def pending_row_clause() -> tuple:
    """Поштова робота, що чекає свій рядок. ОДИН предикат для повтору,
    банера (`sheet_stuck_writes._pending`) і кнопки «Записати зараз»."""
    return (
        Order.source == "email",
        Order.sheet_row_pending.is_not(None),
        Order.row_number.is_(None),
        Order.archived_at.is_(None),
    )


def retry_pending_mail_rows(
    db: Session, *, now: float | None = None, wall: datetime | None = None,
) -> int:
    """Тік синку: поставити в пул дописування рядків, що чекають довше за
    `PENDING_ROW_GRACE_SECONDS`. Повертає, скільки поставлено."""
    from app.services.sheet_writeback import submit_sheet_write
    from app.sheets import quota_is_tight

    if sync_control.is_paused() or quota_is_tight():
        return 0
    now = monotonic() if now is None else now
    wall = wall or datetime.now()
    pending = db.execute(
        select(Order.id, Order.sheet_row_pending).where(*pending_row_clause()).order_by(Order.id)
    ).all()
    alive = {order_id for order_id, _ in pending}
    for stale in [key for key in list(_attempts) if key not in alive]:
        _attempts.pop(stale, None)
    submitted = 0
    for order_id, since in pending:
        if since is not None and (wall - since).total_seconds() < PENDING_ROW_GRACE_SECONDS:
            continue
        last = _attempts.get(order_id)
        if last is not None and now - last < PENDING_ROW_RETRY_SECONDS:
            continue
        _attempts[order_id] = now
        submit_sheet_write(append_pending_mail_row_warm, order_id)
        submitted += 1
        if submitted >= PENDING_ROW_RETRY_BATCH:
            break
    if submitted:
        logger.info("Повтор рядків поштових робіт: поставлено %d (чекають усього %d)",
                    submitted, len(pending))
    return submitted


def retry_pending_mail_rows_now(db: Session, order_ids: set[int], *, now: float | None = None) -> list:
    """Кнопка «Записати зараз»: без вікна очікування — банер показує роботу
    вже ПІСЛЯ збою, тобто прийняття завершилось. Захист від дубля той самий:
    спершу пошук «нічийого» рядка."""
    from app.services.sheet_writeback import submit_sheet_write

    if not order_ids:
        return []
    now = monotonic() if now is None else now
    ids = db.scalars(
        select(Order.id).where(Order.id.in_(order_ids), *pending_row_clause())
    ).all()
    futures = []
    for order_id in ids:
        _attempts[order_id] = now
        futures.append(submit_sheet_write(append_pending_mail_row_warm, order_id))
    return futures


def _orphan_row(db: Session, order: Order, raw: list[list[str]]) -> int | None:
    """Номер «нічийого» рядка цієї роботи у вкладці: той самий ключ identity,
    що в синку, той самий Sum3D (коли він є в обох), і жодна робота бази цей
    рядок не тримає. Кілька таких — перший: вони однакові за змістом."""
    from app.parser import parse_rows
    from app.sync import _order_identity, _row_identity

    key = _order_identity(order)
    if key is None:
        return None
    owned = set(db.scalars(
        select(Order.row_number).where(
            Order.sheet_tab == order.sheet_tab, Order.row_number.is_not(None),
        )
    ).all())
    sum3d = (order.sum3d_id or "").strip()
    for row in sorted(parse_rows(raw), key=lambda r: r.row_number):
        if row.row_number in owned or _row_identity(row) != key:
            continue
        row_sum3d = (row.sum3d_id or "").strip()
        if sum3d and row_sum3d and row_sum3d != sum3d:
            continue
        return row.row_number
    return None


def append_pending_mail_row_warm(order_id: int) -> str | None:
    """На воркері: прив'язати «нічий» рядок або дописати рядок-нотатку.
    Повертає текст помилки або None."""
    from app.parser import header_mismatches
    from app.services.mail_accept import _write_placeholder_row
    from app.services.sheet_writeback import writeback_session
    from app.sheets import call_with_retry, get_worksheet_by_name, open_spreadsheet

    with writeback_session() as bg:
        order = bg.get(Order, order_id)
        if (
            order is None or order.source != "email" or order.row_number is not None
            or order.sheet_row_pending is None or order.archived_at is not None
        ):
            return None
        email = (
            bg.get(EmailMessage, order.source_email_id) if order.source_email_id else None
        ) or SimpleNamespace(id=order.source_email_id)

        worksheet = None
        if order.sheet_tab:
            try:
                worksheet = get_worksheet_by_name(open_spreadsheet(db=bg), order.sheet_tab)
            except Exception as exc:  # noqa: BLE001 — повтор піде знову
                note_write_failed("row", order, str(exc) or type(exc).__name__)
                return str(exc)
        if worksheet is not None:
            try:
                raw = call_with_retry(worksheet.get_all_values)
            except Exception as exc:  # noqa: BLE001 — без звірки не дописуємо
                note_write_failed("row", order, str(exc) or type(exc).__name__)
                return str(exc)
            problems = header_mismatches(raw)
            if problems:
                # Колонки зсунуто — і пошук рядка, і запис лягли б не туди.
                error = "структура вкладки не впізнана: " + "; ".join(problems)
                note_write_failed("row", order, error)
                return error
            found = _orphan_row(bg, order, raw)
            if found is not None:
                order.row_number = found
                order.sheet_row_pending = None
                bg.add(SyncLog(
                    direction="mail_to_sheet", sheet_tab=order.sheet_tab, status="ok",
                    message=(
                        f"email {email.id}: рядок-нотатка вже була в таблиці (відповідь "
                        f"на запис загубилась) — робота {order.id} прив'язана до неї"
                    ),
                ))
                bg.commit()
                note_write_ok("row", order.id)
                return None

        _write_placeholder_row(bg, email, order, worksheet)
        bg.commit()
        return None if order.row_number is not None else "рядок-нотатку не записано"
