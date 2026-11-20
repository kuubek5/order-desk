"""Піч перестала відповідати під час спікання (власник 20.11.26).

Піч живиться від батареї резервного живлення, і батарея може сісти посеред
спікання. Тоді табло печі (VNC) просто мовчить. Правило:

* останній кадр, на якому піч ще відповідала, показував RUN;
* відтоді піч не відповідає щонайменше `OFFLINE_AFTER_SECONDS` (власник: 1 хв;
  кадр раз на 6 с, тож короткий збій мережі сюди не долітає);
* → запис `FurnaceOutage` і повідомлення на всіх екранах CRM, доки хтось не
  натисне «Зрозуміло». Коли піч знову відповіла — у записі час повернення.

Мовчання печі й обрив мережі ззовні однакові, тому текст каже «не відповідає»,
а не «вимкнулась». Статус WAIT (піч не пече) повідомлення не дає ніколи: ніч,
вимкнена на вихідні піч — нормальний стан.

Хто бачить — `feature_access` (доступ від адміна + власний перемикач у
Кабінеті). Знімає будь-хто, у кого повідомлення є: воно спільне.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.furnace_ocr import STATUS_RUN, STATUS_WAIT
from app.models import FurnaceOutage, FurnaceReading, User
from app.services import feature_access

logger = logging.getLogger(__name__)

FEATURE = feature_access.BY_KEY["furnace_power"]

OFFLINE_AFTER_SECONDS = 60.0
# Стан печі, який давно не оновлювався (піч прибрали з налаштувань, воркер
# стоїть), — не привід для повідомлення: ми не знаємо, що з нею.
STALE_STATE_SECONDS = 120.0
# Після рестарту пам'яті немає — останній відомий статус береться з бази, але
# лише свіжий: програма стояла годину — що робила піч, невідомо.
DB_FALLBACK_MAX_AGE = timedelta(minutes=15)

_KNOWN = (STATUS_RUN, STATUS_WAIT)


@dataclass
class _Seen:
    at: datetime          # останній кадр, на якому піч відповідала
    status: str           # останній ВПЕВНЕНИЙ статус (RUN/WAIT)
    temp_c: Optional[int] = None


_last: dict[str, _Seen] = {}
_db_checked: set[str] = set()
_lock = threading.Lock()


def reset_memory() -> None:
    """Для тестів і зміни адреси печі."""
    with _lock:
        _last.clear()
        _db_checked.clear()


def _from_db(db: Session, key: str, now: datetime) -> Optional[_Seen]:
    row = db.scalars(
        select(FurnaceReading)
        .where(FurnaceReading.host == key, FurnaceReading.error.is_(None),
               FurnaceReading.status.in_(_KNOWN))
        .order_by(FurnaceReading.captured_at.desc())
        .limit(1)
    ).first()
    if row is None or now - row.captured_at > DB_FALLBACK_MAX_AGE:
        return None
    return _Seen(at=row.captured_at, status=row.status, temp_c=row.temp_c)


def _note_ok(db: Session, key: str, state: Any) -> None:
    at = state.captured_at
    status = state.status
    with _lock:
        seen = _last.get(key)
        if status in _KNOWN:
            _last[key] = _Seen(at=at, status=status, temp_c=state.temp_c)
        elif seen is not None:
            # Кадр є, статус не впевнений (сигнали розійшлись) — піч жива,
            # статус лишається попередній.
            seen.at = at
            if state.temp_c is not None:
                seen.temp_c = state.temp_c
    open_rows = db.scalars(
        select(FurnaceOutage).where(FurnaceOutage.furnace_key == key, FurnaceOutage.recovered_at.is_(None))
    ).all()
    if open_rows:
        for row in open_rows:
            row.recovered_at = at
        db.commit()
        logger.info("Піч %s знову відповідає (%s)", key, at.strftime("%H:%M:%S"))


def _note_silent(db: Session, key: str, name: str, now: datetime) -> Optional[FurnaceOutage]:
    with _lock:
        seen = _last.get(key)
        need_db = seen is None and key not in _db_checked
        if need_db:
            _db_checked.add(key)
    if need_db:
        seen = _from_db(db, key, now)
        if seen is not None:
            with _lock:
                _last.setdefault(key, seen)
    if seen is None or seen.status != STATUS_RUN:
        return None
    if (now - seen.at).total_seconds() < OFFLINE_AFTER_SECONDS:
        return None
    exists = db.scalars(
        select(FurnaceOutage.id).where(FurnaceOutage.furnace_key == key, FurnaceOutage.started_at == seen.at)
    ).first()
    if exists is not None:
        return None
    row = FurnaceOutage(furnace_key=key, furnace_name=name or key, started_at=seen.at,
                        detected_at=now, last_temp_c=seen.temp_c)
    db.add(row)
    db.commit()
    logger.warning("Піч %s не відповідає з %s під час спікання (%s °C) — повідомлення в CRM",
                   name or key, seen.at.strftime("%H:%M:%S"), seen.temp_c if seen.temp_c is not None else "?")
    return row


def tick(db: Session, states: Optional[dict[str, Any]] = None, now: Optional[datetime] = None) -> None:
    """Прохід із воркера печей, ПІСЛЯ опитування. Ніколи не кидає: повідомлення
    не має права зупинити опитування печей."""
    if states is None:
        from app.services import furnace as furnace_service

        states = furnace_service.states_snapshot()
    moment = now or datetime.now()
    for key, state in states.items():
        try:
            attempted = state.attempted_at
            if attempted is None or (moment - attempted).total_seconds() > STALE_STATE_SECONDS:
                continue
            if state.error is None and state.captured_at is not None:
                _note_ok(db, key, state)
            elif state.error is not None:
                _note_silent(db, key, getattr(state.target, "name", "") or key, moment)
        except Exception:  # noqa: BLE001 — не зупиняти опитування печей
            logger.exception("Піч %s: перевірка зв'язку під час спікання не вдалася", key)
            db.rollback()


# ── показ ───────────────────────────────────────────────────────────────────


@dataclass
class Notice:
    id: int
    name: str
    started_at: datetime
    recovered_at: Optional[datetime]
    last_temp_c: Optional[int]

    @property
    def is_open(self) -> bool:
        return self.recovered_at is None

    def minutes(self, now: datetime) -> int:
        end = self.recovered_at or now
        return max(1, round((end - self.started_at).total_seconds() / 60))


def notices(db: Session) -> list[Notice]:
    rows = db.scalars(
        # Те, що ще триває, — першим.
        select(FurnaceOutage).where(FurnaceOutage.acked_at.is_(None))
        .order_by(FurnaceOutage.recovered_at.is_not(None), FurnaceOutage.started_at)
    ).all()
    return [Notice(id=r.id, name=r.furnace_name or r.furnace_key, started_at=r.started_at,
                   recovered_at=r.recovered_at, last_temp_c=r.last_temp_c) for r in rows]


def notices_for(db: Session, user: Optional[User]) -> list[Notice]:
    if not feature_access.is_on(user, FEATURE):
        return []
    return notices(db)


def acknowledge(db: Session, user: Optional[User], outage_id: int, now: Optional[datetime] = None) -> bool:
    row = db.get(FurnaceOutage, outage_id)
    if row is None:
        return False
    if row.acked_at is None:
        row.acked_at = now or datetime.now()
        row.acked_by_id = getattr(user, "id", None)
        db.commit()
        logger.info("Повідомлення «піч %s не відповідала» знято: %s", row.furnace_name,
                    getattr(user, "username", "?"))
    return True


# ── звірка на історії (MCP) ─────────────────────────────────────────────────


def replay(db: Session, days: int, now: Optional[datetime] = None) -> dict[str, Any]:
    """Що правило сказало б за минулі дні — з рядків `furnace_readings`.

    Рядок помилки пишеться ОДРАЗУ на першій невдачі (далі раз на 15 хв), а
    успішний — щохвилини, тож обрив під час RUN видно як «RUN → помилка».
    Тривалість — до наступного успішного рядка (з точністю до хвилини)."""
    moment = now or datetime.now()
    since = moment - timedelta(days=days)
    rows = db.scalars(
        select(FurnaceReading).where(FurnaceReading.captured_at >= since)
        .order_by(FurnaceReading.host, FurnaceReading.captured_at)
    ).all()
    found: list[dict[str, Any]] = []
    silent_wait = 0
    last_ok: dict[str, FurnaceReading] = {}
    pending: dict[str, FurnaceReading] = {}
    for row in rows:
        key = row.host
        if row.error is None:
            start = pending.pop(key, None)
            if start is not None:
                ok = last_ok.get(key)
                if ok is not None and ok.status == STATUS_RUN:
                    gap = (row.captured_at - ok.captured_at).total_seconds()
                    if gap >= OFFLINE_AFTER_SECONDS:
                        found.append({"піч": key, "з": ok.captured_at.strftime("%d.%m %H:%M:%S"),
                                      "до": row.captured_at.strftime("%d.%m %H:%M:%S"),
                                      "хв": round(gap / 60), "°C": ok.temp_c, "після": row.status})
                else:
                    silent_wait += 1
            if row.status in _KNOWN:
                last_ok[key] = row
            continue
        if key not in pending:
            pending[key] = row
    for key, start in pending.items():
        ok = last_ok.get(key)
        if ok is not None and ok.status == STATUS_RUN:
            found.append({"піч": key, "з": ok.captured_at.strftime("%d.%m %H:%M:%S"), "до": None,
                          "хв": round((moment - ok.captured_at).total_seconds() / 60), "°C": ok.temp_c,
                          "після": None})
    return {
        "днів": days,
        "повідомлень_було_б": len(found),
        "обривів_поза_спіканням": silent_wait,
        "обриви_під_час_спікання": found,
        "пояснення": "Повідомлення — коли останній кадр перед мовчанням показував RUN і піч мовчала ≥ 1 хв. "
                     "Обрив у WAIT (ніч, вимкнена піч) повідомлення не дає.",
    }
