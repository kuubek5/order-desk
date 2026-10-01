"""Запис у таблицю, що застряг: сказати людям, а не лише позначкою в рядку.

Sum3D і синя заливка видачі пишуться в Google у фоні: база тримає значення
одразу, а позначка `Order.sum3d_pending` / `Order.fill_pending` стоїть, доки
таблиця не підтвердила запис. Повтор іде сам кожні 2 хв
(`retry_pending_sum3d`, `retry_pending_fills`). Але якщо запис не доходить
довго (мережа лягла, рядок не звіряється), єдиним сигналом лишався трикутник
у рядку — його видно, лише коли дивишся саме на цей рядок (власник 30.09.26:
«стовідсоткова гарантія, а якщо ні — щоб нам про це повідомило»).

Тут — сторож, який бачить позначки ДОВШЕ за `STUCK_AFTER` і:
* тримає знімок для банера в черзі (`stuck_sheet_writes`, у памʼяті, як
  `mass_vanish_pending`, — банер полліться кожні 15 с і в БД не ходить);
* ОДИН раз на епізод пише в Журнал синку й шле власнику в Telegram;
* коли все дописалось — один запис «дописано» в журнал, епізод закрито.

Час — монотонний і в памʼяті процесу, як `_pending_sum3d_attempts`: після
перезапуску відлік іде заново, і це свідомо — повтори після старту теж
починаються заново, тож «застряг» має рахуватись від них.

Пауза синку: записи не йдуть СВІДОМО, тож і тривоги немає — стан скидається,
а пауза видна власним банером. Скидає САМ воркер синку в гілці паузи
(`web._sheet_sync_worker`): на паузі він до `observe` не доходить, тож
перевірка паузи всередині `observe` — лише запобіжник для інших викликачів.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import sync_control
from app.models import Order, SyncLog

logger = logging.getLogger(__name__)

# Звичайний запис підтверджується за 1–2 с, повтор — кожні 2 хв. П'ять хвилин
# = два повтори не дійшли: це вже не «ось-ось», а «застрягло».
STUCK_AFTER_SECONDS = 5 * 60

# Скільки прикладів показувати в тексті журналу й Telegram.
_EXAMPLES = 5


@dataclass(frozen=True)
class StuckWrites:
    sum3d: int
    fill: int

    @property
    def total(self) -> int:
        return self.sum3d + self.fill


_lock = threading.Lock()
_first_seen: dict[tuple[str, int], float] = {}
_snapshot: StuckWrites | None = None
_episode_open = False


def stuck_sheet_writes() -> StuckWrites | None:
    """Знімок для банера: скільки записів застрягло. None — нічого."""
    with _lock:
        return _snapshot


def reset() -> None:
    """Забути стан. Для тестів і паузи синку."""
    global _snapshot, _episode_open
    with _lock:
        _first_seen.clear()
        _snapshot = None
        _episode_open = False


def _pending(db: Session) -> dict[tuple[str, int], str]:
    """Позначки, що чекають таблиці: {(вид, id): підпис для людини}.

    Предикат Sum3D — той самий, що в `retry_pending_sum3d` і в рядку черги
    (`_order_row.html`): позначка дорівнює поточному ID. Інакше це слід
    старого ID, який повтор не пише і рядок не показує.
    """
    found: dict[tuple[str, int], str] = {}
    for order in db.scalars(
        select(Order).where(
            Order.sum3d_pending.is_not(None),
            Order.sum3d_pending == Order.sum3d_id,
            Order.archived_at.is_(None),
        )
    ):
        found[("sum3d", order.id)] = _label(order)
    for order in db.scalars(
        select(Order).where(
            Order.fill_pending.is_not(None),
            Order.archived_at.is_(None),
        )
    ):
        found[("fill", order.id)] = _label(order)
    return found


def _label(order: Order) -> str:
    who = order.client_name or order.work_order_no or f"#{order.id}"
    return f"{who} ({order.sheet_tab or '—'})"


def observe(db: Session, *, now: float | None = None, wall: datetime | None = None) -> StuckWrites | None:
    """Один тік сторожа. Кличе тік синку таблиці після повторів записів.

    Повертає поточний знімок. Комітить сам, лише коли додав запис у журнал
    чи чергу Telegram."""
    global _snapshot, _episode_open

    if sync_control.is_paused():
        reset()
        return None

    now = monotonic() if now is None else now
    pending = _pending(db)

    with _lock:
        for key in [k for k in _first_seen if k not in pending]:
            del _first_seen[key]
        for key in pending:
            _first_seen.setdefault(key, now)
        stuck = sorted(k for k, t in _first_seen.items() if now - t >= STUCK_AFTER_SECONDS)
        snapshot = (
            StuckWrites(
                sum3d=sum(1 for kind, _ in stuck if kind == "sum3d"),
                fill=sum(1 for kind, _ in stuck if kind == "fill"),
            )
            if stuck else None
        )
        _snapshot = snapshot
        opened = snapshot is not None and not _episode_open
        closed = snapshot is None and _episode_open
        _episode_open = snapshot is not None

    if opened and snapshot is not None:
        _report_stuck(db, snapshot, [pending[k] for k in stuck], keys=stuck, wall=wall)
    elif closed:
        db.add(SyncLog(
            direction="db_to_sheet", sheet_tab=None, status="ok",
            message="Застряглі записи в таблицю дописано — позначок «ще не в таблиці» більше немає",
        ))
        db.commit()
    return snapshot


def _describe(snapshot: StuckWrites) -> str:
    parts = []
    if snapshot.sum3d:
        parts.append(f"Sum3D — {snapshot.sum3d}")
    if snapshot.fill:
        parts.append(f"синя заливка — {snapshot.fill}")
    return ", ".join(parts)


def _telegram_key(keys: list[tuple[str, int]], moment: datetime) -> str:
    """Ключ дедуплікації Telegram: ТОЙ САМИЙ набір застряглих робіт за день.

    Стан сторожа живе в памʼяті процесу, тож після кожного запуску програми
    (на цеховому ПК — щоранку) епізод відкривається знову. Рядок, що не
    звіряється ніколи, інакше приносив би власнику те саме повідомлення щодня
    після кожного старту — а новий набір або новий день приходять як новина.
    """
    digest = hashlib.sha1(repr(sorted(keys)).encode("utf-8")).hexdigest()[:12]
    return f"sheet-stuck:{moment:%Y%m%d}:{digest}"


def _report_stuck(
    db: Session, snapshot: StuckWrites, labels: list[str], *,
    keys: list[tuple[str, int]], wall: datetime | None,
) -> None:
    examples = ", ".join(labels[:_EXAMPLES])
    more = f" і ще {len(labels) - _EXAMPLES}" if len(labels) > _EXAMPLES else ""
    minutes = STUCK_AFTER_SECONDS // 60
    # «Коли дозволяє квота»: повтор свідомо пропускає тік, коли запитів до
    # Google забагато (`quota_is_tight`), — обіцяти «кожні 2 хв» без
    # застереження було б неправдою саме тоді, коли тривога приходить.
    text = (
        f"Не дійшло в Google Таблицю понад {minutes} хв ({_describe(snapshot)}): "
        f"{examples}{more}. База значення тримає, повтор іде сам кожні 2 хв, "
        "коли дозволяє квота Google"
    )
    logger.warning("Застряглі записи в таблицю: %s", text)
    db.add(SyncLog(direction="db_to_sheet", sheet_tab=None, status="warning", message=text))

    # Telegram — лише власнику й лише коли бот увімкнено: інакше рядок черги
    # лежав би до прострочення, нікому не адресований.
    try:
        from app.services import telegram_bot
        from app.services.telegram import KMILL_PREFIX

        if telegram_bot.bot_enabled(db):
            moment = wall or datetime.now()
            telegram_bot.enqueue(
                db,
                dedup_key=_telegram_key(keys, moment),
                kind="sheet_stuck",
                text=f"{KMILL_PREFIX} · ⚠ {text}.",
                now=moment,
                ttl=timedelta(hours=6),
            )
    except Exception:  # noqa: BLE001 — сповіщення не має валити тік синку
        logger.exception("Сповіщення про застряглі записи в Telegram не поставлено")
    db.commit()
