"""Запис у таблицю, що не дійшов: сказати оператору одразу, у самій CRM.

Sum3D і синя заливка видачі пишуться в Google у фоні: база тримає значення
одразу, а позначка `Order.sum3d_pending` / `Order.fill_pending` стоїть, доки
таблиця не підтвердила запис. Повтор іде сам кожні 2 хв
(`retry_pending_sum3d`, `retry_pending_fills`). Але єдиним сигналом збою
лишався трикутник у рядку — його видно, лише коли дивишся саме на цей рядок.

Рішення власника 01.10.26: сигнал — НЕ в Telegram, а в CRM, щоб оператор міг
одразу виправити; з'являється одразу при збої; список робіт із переходом до
рядка, причина простими словами, кнопка «Записати зараз».

Два джерела списку:
* **збій** — місце запису (`write_sheet_fields`, `write_fields_bulk`,
  фарбування рядка) саме каже, що не дійшло й чому (`note_write_failed`), і
  каже, коли дійшло (`note_write_ok`). Позначка в БД стоїть і під час
  нормального запису (1–2 с), тож «позначка є» саме по собі ще не збій;
* **без причини** — позначка висить довше за `STUCK_AFTER`, а збою ніхто не
  повідомив (наприклад, після перезапуску, коли повтор ще не встиг через
  квоту). Тоді причина «невідома», але робота в списку є.

Стан у памʼяті процесу, як `_pending_sum3d_attempts`: перезапуск забуває
причини, але повтор після старту йде на першому ж тіку й повідомляє їх знову.
Позначки — у БД, тож самі роботи не губляться.

Журнал синку: ОДИН запис на епізод, коли щось висить довше `STUCK_AFTER`, і
один «дописано», коли все дійшло (самі збої журнал і так пише по рядку).

Пауза синку: записи не йдуть СВІДОМО, тож і тривоги немає — стан скидається,
а пауза видна власним банером. Скидає САМ воркер синку в гілці паузи
(`web._sheet_sync_worker`): на паузі він до `observe` не доходить, тож
перевірка паузи всередині `observe` — лише запобіжник для інших викликачів.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import sync_control
from app.models import Order, SyncLog

logger = logging.getLogger(__name__)

# Звичайний запис підтверджується за 1–2 с, повтор — кожні 2 хв. П'ять хвилин
# = два повтори не дійшли: це вже не «ось-ось», а «застрягло».
STUCK_AFTER_SECONDS = 5 * 60

# Збій, про який щойно повідомили, не прибираємо з першого ж тіку, навіть
# якщо позначки в БД ще не видно: місце запису комітить позначку ПІСЛЯ
# виклику `note_write_failed`, і тік між ними стер би свіжий збій.
_FAILURE_GRACE_SECONDS = 15.0

# Скільки прикладів показувати в тексті журналу.
_EXAMPLES = 5

# Причини простими словами — для банера. Ключ — `FailedWrite.reason`.
REASON_TEXT = {
    "row": "рядок не знайдено в таблиці однозначно — виправте рядок у таблиці",
    "tab": "вкладки немає в таблиці — перевірте назву вкладки",
    "net": "немає зв'язку з Google — повтор іде сам",
    "unknown": "таблиця ще не підтвердила запис — повтор іде сам",
}

KIND_TEXT = {"sum3d": "Sum3D", "fill": "синя заливка", "row": "рядок роботи"}


@dataclass(frozen=True)
class FailedWrite:
    kind: str          # "sum3d" | "fill" | "row" (рядок-нотатка поштової роботи)
    order_id: int
    who: str           # клієнт, наряд або #id — як людина шукає рядок
    tab: str | None    # вкладка (дд.мм.рр), щоб перехід відкрив її день
    reason: str        # ключ REASON_TEXT
    detail: str        # сирий текст помилки — у підказку
    since: datetime    # коли вперше не дійшло (місцевий час)

    @property
    def reason_text(self) -> str:
        return REASON_TEXT.get(self.reason, REASON_TEXT["unknown"])

    @property
    def kind_text(self) -> str:
        return KIND_TEXT.get(self.kind, self.kind)


@dataclass(frozen=True)
class StuckWrites:
    """Підсумок «висить довше 5 хв» — для журналу синку."""
    sum3d: int
    fill: int
    row: int = 0

    @property
    def total(self) -> int:
        return self.sum3d + self.fill + self.row


_lock = threading.Lock()
# Збої, про які повідомило місце запису: ключ → (запис, monotonic-час).
_failures: dict[tuple[str, int], tuple[FailedWrite, float]] = {}
# Позначки, що висять довше STUCK_AFTER без повідомленого збою.
_unknown: dict[tuple[str, int], FailedWrite] = {}
_first_seen: dict[tuple[str, int], float] = {}
_first_seen_wall: dict[tuple[str, int], datetime] = {}
_snapshot: StuckWrites | None = None
_episode_open = False


def classify(error: str | None) -> str:
    """Причина збою простими словами — за текстом, який повертають `write_*`."""
    text = (error or "").lower()
    if "не підтверджено" in text:
        return "row"
    if "вкладк" in text and "не знайдено" in text:
        return "tab"
    return "net"


def _who(order: Order) -> str:
    return (
        getattr(order, "client_name", None)
        or getattr(order, "work_order_no", None)
        or f"#{order.id}"
    )


def note_write_failed(
    kind: str, order: Order, error: str | None, *,
    now: float | None = None, wall: datetime | None = None,
) -> None:
    """Місце запису: не дійшло. Час першого збою епізоду зберігається, причина
    — найсвіжіша (мережа могла повернутись, а рядок лишився незвіреним).

    Ніколи не кидає: це сигнал ПРО запис, і впасти тут означало б зламати сам
    запис (позначку, коміт, журнал) — гірше, ніж не показати банер."""
    try:
        if sync_control.is_paused():
            return
        key = (kind, order.id)
        now = monotonic() if now is None else now
        item_tab = getattr(order, "sheet_tab", None)
        with _lock:
            prev = _failures.get(key)
            since = prev[0].since if prev else (wall or datetime.now())
            _failures[key] = (
                FailedWrite(
                    kind=kind, order_id=order.id, who=_who(order), tab=item_tab,
                    reason=classify(error), detail=(error or "")[:300], since=since,
                ),
                now,
            )
            _unknown.pop(key, None)
    except Exception:  # noqa: BLE001 — див. докстрінг
        logger.exception("Сигнал «не дійшло в таблицю» не записано")


def note_write_ok(kind: str, order_id: int) -> None:
    """Місце запису: таблиця підтвердила."""
    key = (kind, order_id)
    with _lock:
        _failures.pop(key, None)
        _unknown.pop(key, None)
        _first_seen.pop(key, None)
        _first_seen_wall.pop(key, None)


def failed_sheet_writes() -> list[FailedWrite]:
    """Список для банера: збої й позначки, що висять без причини. Найстаріші
    перші — вони найдовше розходяться з таблицею."""
    with _lock:
        items = [item for item, _ in _failures.values()]
        items += [item for key, item in _unknown.items() if key not in _failures]
    return sorted(items, key=lambda item: (item.since, item.kind, item.order_id))


def stuck_sheet_writes() -> StuckWrites | None:
    """Підсумок «висить довше 5 хв». None — нічого."""
    with _lock:
        return _snapshot


def reset() -> None:
    """Забути стан. Для тестів і паузи синку."""
    global _snapshot, _episode_open
    with _lock:
        _failures.clear()
        _unknown.clear()
        _first_seen.clear()
        _first_seen_wall.clear()
        _snapshot = None
        _episode_open = False


def _pending(db: Session) -> dict[tuple[str, int], Order]:
    """Позначки, що чекають таблиці: {(вид, id): робота}.

    Предикат Sum3D — той самий, що в `retry_pending_sum3d` і в рядку черги
    (`_order_row.html`): позначка дорівнює поточному ID. Інакше це слід
    старого ID, який повтор не пише і рядок не показує.
    """
    found: dict[tuple[str, int], Order] = {}
    for order in db.scalars(
        select(Order).where(
            Order.sum3d_pending.is_not(None),
            Order.sum3d_pending == Order.sum3d_id,
            Order.archived_at.is_(None),
        )
    ):
        found[("sum3d", order.id)] = order
    for order in db.scalars(
        select(Order).where(
            Order.fill_pending.is_not(None),
            Order.archived_at.is_(None),
        )
    ):
        found[("fill", order.id)] = order
    # Поштова робота без рядка-нотатки — той самий предикат, що в повторі
    # (`mail_row_retry.pending_row_clause`).
    from app.services.mail_row_retry import pending_row_clause

    for order in db.scalars(select(Order).where(*pending_row_clause())):
        found[("row", order.id)] = order
    return found


def _label(order: Order) -> str:
    return f"{_who(order)} ({order.sheet_tab or '—'})"


def observe(db: Session, *, now: float | None = None, wall: datetime | None = None) -> StuckWrites | None:
    """Один тік сторожа. Кличе тік синку таблиці після повторів записів.

    Звіряє список з позначками в БД: збій, чия позначка зникла (дописав
    повтор, синк побачив значення в таблиці, робота пішла в архів), зі
    списку йде. Повертає підсумок «висить довше 5 хв». Комітить сам, лише
    коли додав запис у журнал."""
    global _snapshot, _episode_open

    if sync_control.is_paused():
        reset()
        return None

    now = monotonic() if now is None else now
    wall_now = wall or datetime.now()
    pending = _pending(db)

    with _lock:
        for key in [k for k, (_, noted) in _failures.items()
                    if k not in pending and now - noted >= _FAILURE_GRACE_SECONDS]:
            del _failures[key]
        for key in [k for k in _first_seen if k not in pending]:
            del _first_seen[key]
            _first_seen_wall.pop(key, None)
        for key in pending:
            _first_seen.setdefault(key, now)
            _first_seen_wall.setdefault(key, wall_now)
        stuck = sorted(k for k, t in _first_seen.items() if now - t >= STUCK_AFTER_SECONDS)
        _unknown.clear()
        for key in stuck:
            if key in _failures:
                continue
            order = pending[key]
            _unknown[key] = FailedWrite(
                kind=key[0], order_id=order.id, who=_who(order), tab=order.sheet_tab,
                reason="unknown", detail="", since=_first_seen_wall[key],
            )
        snapshot = (
            StuckWrites(
                sum3d=sum(1 for kind, _ in stuck if kind == "sum3d"),
                fill=sum(1 for kind, _ in stuck if kind == "fill"),
                row=sum(1 for kind, _ in stuck if kind == "row"),
            )
            if stuck else None
        )
        _snapshot = snapshot
        opened = snapshot is not None and not _episode_open
        closed = snapshot is None and _episode_open
        _episode_open = snapshot is not None

    if opened and snapshot is not None:
        _report_stuck(db, snapshot, [_label(pending[k]) for k in stuck])
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
    if snapshot.row:
        parts.append(f"рядок поштової роботи — {snapshot.row}")
    return ", ".join(parts)


def _report_stuck(db: Session, snapshot: StuckWrites, labels: list[str]) -> None:
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
    db.commit()
