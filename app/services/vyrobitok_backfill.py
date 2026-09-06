"""Точковий добір СЛМ у табель «Виробіток» за вже минулий місяць.

Навіщо. СЛМ не потрапляє в `Order` (його рядки викидає `_is_non_queue_row`),
тому числа табеля пише синк тим самим проходом, що читає вкладку
(`sync_tab` → `store_slm_totals`). Але рутинний синк читає лише сьогодні±1 —
день, чия вкладка вже вийшла з вікна ДО того, як на машині зʼявився код
запису СЛМ (v0.8.23, 05.09.26), лишається з порожніми СЛМ-колонками назавжди.
Саме так виглядає «записався лише один день».

Наявна дія «Імпортувати всю історію» це лікує, але вона важка: перечитує ВСІ
вкладки документа й ганяє повну реконсиляцію черги (десятки хвилин через
проксі лабораторії). Тут — вузька операція: вкладки ОДНОГО місяця, читання
без імпорту, запис лише в клітинки `lab_slm`/`mail_slm`. Черга, архів,
реконсиляція видалень не зачіпаються взагалі — жодного запису в `Order`.

Правку оператора (`override_value`) добір не чіпає: `store_slm_totals` пише
тільки `auto_value`.
"""

from __future__ import annotations

import calendar
import logging
import time
from dataclasses import dataclass
from datetime import date
from threading import Lock, Thread

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.parser import HEADER_ROWS, header_mismatches, parse_rows
from app.services.order_dates import parse_sheet_tab
from app.services.vyrobitok import slm_totals_from_rows, store_slm_totals
from app.sheets import call_with_retry, open_spreadsheet, quota_is_tight

logger = logging.getLogger(__name__)

# Пауза, коли квота Google близька до межі (60 викликів/хв). Місяць — це до 31
# читання поспіль, тож без гальма прогін впирається в 429 і йде в ретраї.
_QUOTA_COOLDOWN_SECONDS = 5.0


@dataclass
class SlmBackfillResult:
    tabs: int = 0            # вкладок прочитано й записано
    skipped_empty: int = 0   # читання без рядків даних (транзієнтний збій/порожня вкладка)
    skipped_header: int = 0  # структура вкладки не впізнана
    lab_units: int = 0
    mail_units: int = 0

    def message(self) -> str:
        parts = [
            f"СЛМ добрано за {self.tabs} вкладок: "
            f"лабораторія {self.lab_units}, пошта {self.mail_units} од."
        ]
        if self.skipped_header:
            parts.append(f"Пропущено (структура не впізнана): {self.skipped_header}.")
        if self.skipped_empty:
            parts.append(f"Пропущено (порожнє читання): {self.skipped_empty}.")
        return " ".join(parts)


def backfill_slm_month(session: Session, year: int, month: int) -> SlmBackfillResult:
    """Перечитати вкладки місяця й записати СЛМ у клітинки табеля.

    Нічого не імпортує і нічого не видаляє: з вкладки береться лише нижній
    блок СЛМ. Порожнє читання пропускається (той самий запобіжник, що в
    `sync_tab`: транзієнтний збій проксі не має затирати реальне число нулем).
    """
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])

    spreadsheet = open_spreadsheet(db=session)
    dated: list[tuple[date, object]] = []
    for worksheet in call_with_retry(spreadsheet.worksheets):
        tab_date = parse_sheet_tab(worksheet.title)
        if tab_date is not None and first <= tab_date <= last:
            dated.append((tab_date, worksheet))
    dated.sort(key=lambda item: item[0])

    result = SlmBackfillResult()
    for tab_date, worksheet in dated:
        if quota_is_tight():
            time.sleep(_QUOTA_COOLDOWN_SECONDS)
        raw = call_with_retry(worksheet.get_all_values)
        if len(raw) < HEADER_ROWS:
            result.skipped_empty += 1
            continue
        problems = header_mismatches(raw)
        if problems:
            logger.warning(
                "добір СЛМ: вкладка %s пропущена — %s",
                worksheet.title, "; ".join(problems),
            )
            result.skipped_header += 1
            continue
        rows = parse_rows(raw)
        lab_units, mail_units = slm_totals_from_rows(rows)
        store_slm_totals(session, tab_date, lab_units, mail_units)
        session.commit()
        result.tabs += 1
        result.lab_units += lab_units
        result.mail_units += mail_units
    return result


# ── Фоновий запуск ───────────────────────────────────────────────────────────
# Місяць — це до 31 читання через проксі лабораторії; інлайн у запиті вкладка
# просто висіла б без фідбеку. Тому демон-потік із власною сесією, а результат
# лягає одноразовим повідомленням, яке екран показує наступним рендером.
_state_lock = Lock()
_running = False
_flash: dict | None = None


def slm_backfill_running() -> bool:
    with _state_lock:
        return _running


def pop_slm_backfill_flash() -> dict | None:
    """Забрати (одноразово) підсумок завершеного добору."""
    global _flash
    with _state_lock:
        flash, _flash = _flash, None
        return flash


def start_slm_backfill(year: int, month: int) -> bool:
    """Почати добір у фоні. False — уже виконується (кнопку натиснули двічі)."""
    global _running
    with _state_lock:
        if _running:
            return False
        _running = True
    Thread(
        target=_run_slm_backfill,
        args=(year, month),
        name="vyrobitok-slm-backfill",
        daemon=True,
    ).start()
    return True


def _run_slm_backfill(year: int, month: int) -> None:
    global _running, _flash
    try:
        with SessionLocal() as session:
            result = backfill_slm_month(session, year, month)
        flash = {"kind": "success", "message": result.message()}
    except Exception as exc:  # noqa: BLE001 — у флеш іде текст, не трейсбек
        logger.exception("добір СЛМ за %s.%s впав", month, year)
        flash = {"kind": "error", "message": f"Добір СЛМ не вдався: {exc}"}
    finally:
        with _state_lock:
            _running = False
            _flash = flash
