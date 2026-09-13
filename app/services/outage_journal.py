"""Слід про обрив зв'язку в Журналі синку.

Навіщо. Фонові мережеві збої не потрапляли в журнал узагалі: `_record_failure`
персистить рядок лише для РУЧНОГО синку (`persist=trigger == "manual"`), а
фоновий тік лишав по собі тільки `logger.warning` у файлі й помилку в пульсі.
Пульс же зеленіє на першому ж успіху — тож після повернення зв'язку сліду не
лишалось ЖОДНОГО. Питання «що саме не доїхало, поки не було інтернету»
відповіді не мало: журнал порожній, пульс зелений.

Чому не просто «персистити завжди». Тік ходить раз на 60 секунд, а обрив
триває годинами — за ніч це сотні однакових рядків, і серед них потонуло б
усе інше. Тому діє те саме правило, що для шумних сторожів застосунку
(`app/log_throttle.py`): перший раз пишемо, далі не частіше ніж раз на годину,
і в тексті видно, скільки спроб було відтоді.

Головний рядок тут — не про збій, а про ВІДНОВЛЕННЯ: саме він закриває вікно
й каже, скільки воно тривало. Без нього журнал показував би початок аварії без
її кінця, і зрозуміти, чи вона ще триває, було б неможливо.

Стан у пам'яті процесу — як і в `log_throttle`: рестарт означає «розкажи
заново». Рестарт сам по собі видно в лозі, тож втраченим вікно не виглядає.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from sqlalchemy.orm import Session

from app import log_throttle
from app.business_day import utc_now
from app.models import SyncLog

logger = logging.getLogger(__name__)

# Напрямок у журналі для кожного джерела (див. DIRECTION_LABELS у
# app/routers/sync_journal.py — рядки там мусять збігатися, інакше запис
# покажеться сирим ключем).
_DIRECTIONS = {"sheet": "sheet_to_db", "mail": "mail_to_db"}

_lock = threading.Lock()
# джерело → (коли почався обрив, скільки спроб було)
_outages: dict[str, tuple[datetime, int]] = {}


def _key(kind: str) -> str:
    return f"outage_journal:{kind}"


def _human(delta_seconds: float) -> str:
    """«12 хв» / «2 год 5 хв» — тривалість, а не мітка часу."""
    minutes = max(1, int(delta_seconds // 60))
    if minutes < 60:
        return f"{minutes} хв"
    return f"{minutes // 60} год {minutes % 60} хв"


def _write(db: Session | None, direction: str, status: str, message: str) -> None:
    """Рядок у журнал. Журнал — страховка, і впасти на ньому означало б зламати
    сам синк, який ми тільки описуємо. Тому тут не може кинути НІЩО:

    * `db is None` — тік викликають і так (наприклад, у тестах пульсу), і це
      не привід валити його;
    * відкат обгорнутий ОКРЕМО: на мертвій чи підробленій сесії падає вже він,
      і тоді виняток вилітав би з самого обробника помилки — страховка ламала б
      те, від чого страхує.
    """
    if db is None:
        return
    try:
        db.add(SyncLog(direction=direction, status=status, message=message))
        db.commit()
    except Exception:
        logger.exception("Не вдалося записати слід про обрив у журнал")
        try:
            db.rollback()
        except Exception:
            logger.exception("Відкат після невдалого запису в журнал теж не вдався")


def note_failure(db: Session, *, kind: str, message: str) -> None:
    """Спроба фонового синку впала. Перший раз пишемо одразу, далі — раз на
    годину, з числом спроб відтоді."""
    direction = _DIRECTIONS.get(kind)
    if direction is None:
        return
    now = utc_now()
    with _lock:
        started, attempts = _outages.get(kind, (now, 0))
        attempts += 1
        _outages[kind] = (started, attempts)
        first = attempts == 1

    # `due` рахує пропущені САМ, тож окремого лічильника для тексту не треба;
    # None означає «нещодавно вже писали».
    skipped = log_throttle.due(_key(kind))
    if first:
        _write(db, direction, "error", f"Зв'язок втрачено: {message}")
        return
    if skipped is None:
        return
    lasted = _human((now - started).total_seconds())
    _write(
        db, direction, "error",
        f"Обрив триває {lasted}, невдалих спроб: {attempts}. {message}",
    )


def note_recovery(db: Session, *, kind: str) -> None:
    """Спроба фонового синку пройшла. Якщо перед цим був обрив — закриваємо
    вікно одним рядком; якщо ні — мовчимо, бо «успіх» і так є нормою."""
    direction = _DIRECTIONS.get(kind)
    if direction is None:
        return
    with _lock:
        outage = _outages.pop(kind, None)
    if outage is None:
        return
    log_throttle.clear(_key(kind))
    started, attempts = outage
    lasted = _human((utc_now() - started).total_seconds())
    _write(
        db, direction, "ok",
        f"Зв'язок відновлено. Обрив тривав {lasted}, невдалих спроб: {attempts}",
    )


def reset() -> None:
    """Забути стан — лише для тестів."""
    with _lock:
        for kind in list(_outages):
            log_throttle.clear(_key(kind))
        _outages.clear()
