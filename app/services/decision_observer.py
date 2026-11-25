"""Спостерігач журналу рішень: що сталося з листом і роботою ПІСЛЯ показаного.

Раз на хвилину порівнює стан листів і робіт із тим, який бачив минулого разу
(`state` у `decisions.db`), і пише різницю подіями. Чому порівнянням, а не
викликом з кожного роута: долю листа міняє десяток місць (кнопки, масова дія,
синк скриньки, відкат), і нове одинадцяте мовчки лишилось би без запису. Різницю
стану «забути» не можна, і рестарт її не губить.

Базу CRM лише ЧИТАЄ. Перший прохід запам'ятовує, що є, і подій не пише:
інакше історія на момент встановлення виглядала б рішеннями одного дня.

Хто змінив — за трьома ознаками, інакше чесне «невідомо»:
  * рядок `ActionLog` тієї самої роботи й поля у вікні між проходами — оператор;
  * `sheet_changed_at` зрушив — людина в таблиці;
  * роут листа назвав користувача (`decisions.note_actor`) — оператор.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import decisions_store as store
from app.__version__ import VERSION
from app.models import ActionLog, Attachment, EmailMessage, Order, ReworkRecord, SelectionRecord
from app.services import decisions as d

logger = logging.getLogger(__name__)

WINDOW_DAYS = 35
"""За скільки днів дивитись. Трохи більше за вікно черги (30) і синку пошти."""

ORDER_FIELDS = {
    "quantity": "кількість",
    "material_color": "матеріал",
    "client_name": "клієнт",
    "sum3d_id": "sum3d",
    "status": "статус",
    "opak_units": "опак",
    "mill_count": "який раз",
    "milled_raw": "де лежить",
    "found_units": "знайдено одиниць",
    "sheet_tab": "вкладка",
}
"""Коментар, вид роботи, шлях і технік сюди не входять: у них імена людей."""

ACTION_FIELDS = {"client": "client_name"}
"""Назва поля в ActionLog, коли вона не збігається з колонкою."""


def _order_state(row: Any) -> dict[str, Any]:
    state = {name: getattr(row, name) for name in ORDER_FIELDS}
    state["архів"] = row.archived_at is not None
    state["src"] = row.source
    state["email"] = row.source_email_id
    # Лише для того, щоб сказати «змінили в таблиці» — самою подією не є.
    state["_sheet"] = row.sheet_changed_at.isoformat() if row.sheet_changed_at else None
    return state


def _letter_state(row: Any) -> dict[str, Any]:
    return {
        "статус": row.status,
        "робота": row.order_id,
        "пауза": row.hold_reason if row.hold_at else None,
        "фільтр": row.filter_category,
        "папка": bool(row.mailbox_folder),
        "зник": row.inbox_gone_at is not None,
        "повернуто": row.reopened_at is not None,
        "файли": row.attachments_status,
    }


def _load_state(conn: sqlite3.Connection, entity: str) -> dict[int, dict[str, Any]]:
    return {
        row["id"]: json.loads(row["body"])
        for row in conn.execute("SELECT id, body FROM state WHERE entity = ?", (entity,))
    }


def _save_state(conn: sqlite3.Connection, entity: str, changed: dict[int, dict[str, Any]]) -> None:
    conn.executemany(
        "INSERT INTO state (entity, id, body) VALUES (?, ?, ?) "
        "ON CONFLICT(entity, id) DO UPDATE SET body = excluded.body",
        [(entity, key, json.dumps(value, ensure_ascii=False)) for key, value in changed.items()],
    )


def _meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _event(now: datetime, step: str, actor: str, **fields: Any) -> dict[str, Any]:
    return {"at": now.isoformat(timespec="seconds"), "step": step, "actor": actor,
            "version": VERSION, **fields}


def _text(value: Any) -> str:
    return "" if value is None else str(value)[:160]


def observe(db: Session, *, now: Optional[datetime] = None) -> int:
    """Один прохід. Повертає, скільки подій записано."""
    if not store.enabled():
        return 0
    now = now or datetime.now()
    # Спершу прочитати все з бази CRM, і лише потім відкривати журнал на
    # запис: блокування журналу не має тривати, поки читається CRM.
    orders = _read_orders(db, now)
    letters = _read_letters(db, now)
    with closing(store.connect(store.current_path())) as conn:
        first = _meta(conn, "baseline") is None
        known_orders = _load_state(conn, "order")
        known_letters = _load_state(conn, "letter")
        last_action = int(_meta(conn, "action_id") or 0)
        last_rework = int(_meta(conn, "rework_id") or 0)
    acted, max_action = _actions(db, last_action, first, now)
    reworks, max_rework = _reworks(db, last_rework, first)

    events: list[dict[str, Any]] = []
    changed_orders = _diff_orders(orders, known_orders, acted, now, events, first)
    fresh = [key for key in letters if key not in known_letters]
    newly_ready = [
        key for key, state in letters.items()
        if state["файли"] == "ready" and (known_letters.get(key) or {}).get("файли") != "ready"
    ]
    facts = {} if first else _letter_facts(db, newly_ready)
    changed_letters = _diff_letters(letters, known_letters, facts, set(fresh), now, events, first)
    for rework in reworks:
        events.append(_event(
            now, d.STEP_REWORK, d.ACTOR_SHEET, order_id=rework.order_id, src=f"rework:{rework.id}",
            data={"який": rework.occurrence, "винен": _blame(rework.blame)},
        ))
    events.extend(_actual_discs(db, now, first))

    with closing(store.connect(store.current_path())) as conn, conn:
        store.insert_events(conn, events)
        _save_state(conn, "order", changed_orders)
        _save_state(conn, "letter", changed_letters)
        _set_meta(conn, "action_id", max_action)
        _set_meta(conn, "rework_id", max_rework)
        if first:
            _set_meta(conn, "baseline", now.isoformat(timespec="seconds"))
        gone = [(key,) for key in known_letters if key not in letters]
        conn.executemany("DELETE FROM state WHERE entity = 'letter' AND id = ?", gone)
        gone = [(key,) for key in known_orders if key not in orders]
        conn.executemany("DELETE FROM state WHERE entity = 'order' AND id = ?", gone)
    return len(events)


def _read_orders(db: Session, now: datetime) -> dict[int, dict[str, Any]]:
    columns = [getattr(Order, name) for name in ORDER_FIELDS]
    rows = db.execute(
        select(Order.id, Order.source, Order.source_email_id, Order.archived_at, Order.sheet_changed_at, *columns)
        .where(Order.created_at >= now - timedelta(days=WINDOW_DAYS))
    ).all()
    return {row.id: _order_state(row) for row in rows}


def _read_letters(db: Session, now: datetime) -> dict[int, dict[str, Any]]:
    rows = db.execute(
        select(
            EmailMessage.id, EmailMessage.status, EmailMessage.order_id, EmailMessage.hold_at,
            EmailMessage.hold_reason, EmailMessage.filter_category, EmailMessage.mailbox_folder,
            EmailMessage.inbox_gone_at, EmailMessage.reopened_at, EmailMessage.attachments_status,
        ).where(EmailMessage.created_at >= now - timedelta(days=WINDOW_DAYS))
    ).all()
    return {row.id: _letter_state(row) for row in rows}


def _actions(db: Session, last_id: int, first: bool, now: datetime) -> tuple[dict[tuple[int, str], Optional[int]], int]:
    """(робота, поле) → хто змінив, за рядками журналу дій після минулого проходу."""
    top = int(db.scalar(select(func.max(ActionLog.id))) or 0)
    if first:
        return {}, top
    rows = db.execute(
        select(ActionLog.order_id, ActionLog.field, ActionLog.action_type, ActionLog.operator_id)
        .where(
            # Останні хвилини читаємо вдруге: роботи прочитано РАНІШЕ за журнал, і
            # дія між цими двома читаннями дасть різницю лише наступного разу.
            (ActionLog.id > last_id) | (ActionLog.created_at >= now - timedelta(minutes=3)),
            ActionLog.order_id.is_not(None),
        )
    ).all()
    acted: dict[tuple[int, str], Optional[int]] = {}
    for row in rows:
        name = ACTION_FIELDS.get(row.field or "", row.field or "")
        acted[(row.order_id, name)] = row.operator_id
        acted[(row.order_id, "*")] = row.operator_id
    # Журнал дій чистять за строком, і номер може виявитись меншим за запам'ятований.
    return acted, max(top, last_id)


def _reworks(db: Session, last_id: int, first: bool) -> tuple[list[ReworkRecord], int]:
    top = int(db.scalar(select(func.max(ReworkRecord.id))) or 0)
    if first or top <= last_id:
        return [], max(top, last_id)
    rows = db.scalars(select(ReworkRecord).where(ReworkRecord.id > last_id)).all()
    return list(rows), top


def _blame(raw: Optional[str]) -> str:
    """Винуватець лише словом зі словника: у клітинку вписують і прізвища."""
    text = (raw or "").casefold()
    for word in ("обладнання", "технік", "адміністратор", "клієнт"):
        if word[:6] in text:
            return word
    return ""


def _diff_orders(
    now_state: dict[int, dict[str, Any]], known: dict[int, dict[str, Any]],
    acted: dict[tuple[int, str], Optional[int]], now: datetime,
    events: list[dict[str, Any]], first: bool,
) -> dict[int, dict[str, Any]]:
    changed: dict[int, dict[str, Any]] = {}
    for order_id, state in now_state.items():
        old = known.get(order_id)
        if old == state:
            continue
        changed[order_id] = state
        if first:
            continue
        if old is None:
            events.append(_event(
                now, d.STEP_ORDER_NEW, d.ACTOR_SYSTEM, order_id=order_id, email_id=state["email"],
                src=f"order-new:{order_id}",
                chosen={ORDER_FIELDS[k]: _text(state[k]) for k in ("client_name", "material_color", "quantity", "sum3d_id")},
                data={"джерело": state["src"]},
            ))
            continue
        for name, label in [*ORDER_FIELDS.items(), ("архів", "архів")]:
            if old.get(name) == state.get(name):
                continue
            if (order_id, name) in acted:
                actor, user_id = d.ACTOR_OPERATOR, acted[(order_id, name)]
            elif old.get("_sheet") != state.get("_sheet"):
                actor, user_id = d.ACTOR_SHEET, None
            elif name in ("архів", "вкладка"):
                actor, user_id = d.ACTOR_SYSTEM, None
            elif (order_id, "*") in acted:
                actor, user_id = d.ACTOR_OPERATOR, acted[(order_id, "*")]
            else:
                actor, user_id = d.ACTOR_UNKNOWN, None
            events.append(_event(
                now, d.STEP_FIELD, actor, user_id=user_id, order_id=order_id, email_id=state["email"],
                rule=label, proposed=_text(old.get(name)), chosen=_text(state.get(name)),
            ))
    return changed


def _fate(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Різниця стану листа — словами цеху."""
    out = []
    if old.get("статус") != new["статус"]:
        out.append({"прийнято": "прийнято", "відхилено": "відхилено", "нове": "знову нове"}.get(
            new["статус"], str(new["статус"])))
    if old.get("пауза") != new["пауза"]:
        out.append(f"на уточненні: {new['пауза']}" if new["пауза"] else "знято з уточнення")
    if old.get("фільтр") != new["фільтр"]:
        out.append(f"у фільтр: {new['фільтр']}" if new["фільтр"] else "знято з фільтра")
    if old.get("папка") != new["папка"]:
        out.append("перенесено в папку скриньки" if new["папка"] else "повернуто з папки скриньки")
    if old.get("зник") != new["зник"]:
        out.append("покинув Вхідні" if new["зник"] else "знову у Вхідних")
    if old.get("повернуто") != new["повернуто"]:
        out.append("прийнятий повернули у Вхідні" if new["повернуто"] else "повернення розібрано")
    return out


def _diff_letters(
    now_state: dict[int, dict[str, Any]], known: dict[int, dict[str, Any]],
    facts: dict[int, dict[str, Any]], fresh: set[int], now: datetime,
    events: list[dict[str, Any]], first: bool,
) -> dict[int, dict[str, Any]]:
    changed: dict[int, dict[str, Any]] = {}
    for email_id, state in now_state.items():
        old = known.get(email_id)
        if old == state:
            continue
        changed[email_id] = state
        if first:
            continue
        if email_id in facts:
            events.append(_event(
                now, d.STEP_LETTER, d.ACTOR_SYSTEM, email_id=email_id,
                data=facts[email_id], src=f"letter:{email_id}:{facts[email_id]['файли']['файлів']}",
            ))
        if email_id in fresh:
            if state["фільтр"]:
                events.append(_event(now, d.STEP_FATE, d.ACTOR_SYSTEM, email_id=email_id,
                                     chosen=f"у фільтр: {state['фільтр']}"))
            continue
        for change in _fate(old or {}, state):
            # Синк скриньки теж міняє стан листа; людину називаємо лише тоді,
            # коли роут листа назвав її щойно.
            user_id = d.actor_of("letter", email_id)
            events.append(_event(
                now, d.STEP_FATE, d.ACTOR_OPERATOR if user_id else d.ACTOR_UNKNOWN,
                user_id=user_id, email_id=email_id, order_id=state["робота"], chosen=change,
            ))
    return changed


def _letter_facts(db: Session, email_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Що розпізнано в листі, щойно його файли скачались."""
    ids = list(email_ids)
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start: start + 400]
        names: dict[int, list[str]] = {}
        for email_id, filename in db.execute(
            select(Attachment.email_message_id, Attachment.filename)
            .where(Attachment.email_message_id.in_(chunk))
        ):
            names.setdefault(email_id, []).append(filename)
        for row in db.execute(
            select(
                EmailMessage.id, EmailMessage.client_name_guess, EmailMessage.material_color_guess,
                EmailMessage.quantity_guess, EmailMessage.service_type_guess,
            ).where(EmailMessage.id.in_(chunk))
        ):
            out[row.id] = {
                "клієнта_вгадано": bool(row.client_name_guess),
                "матеріал_вгадано": bool(row.material_color_guess),
                "кількість_у_листі": row.quantity_guess if (row.quantity_guess or "").isdigit() else None,
                "послуга": row.service_type_guess,
                "файли": d.file_facts(names.get(row.id, [])),
            }
    return out


def _actual_discs(db: Session, now: datetime, first: bool) -> list[dict[str, Any]]:
    """Фактичний диск, який дописав добовий прохід журналу підбору."""
    if first:
        return []
    rows = db.execute(
        select(
            SelectionRecord.id, SelectionRecord.order_id, SelectionRecord.email_id,
            SelectionRecord.actual_disc, SelectionRecord.actual_height,
        ).where(
            SelectionRecord.actual_disc.is_not(None),
            SelectionRecord.actual_at >= now - timedelta(days=2),
        )
    ).all()
    return [
        _event(now, d.STEP_DISC_ACTUAL, d.ACTOR_OPERATOR, order_id=row.order_id, email_id=row.email_id,
               chosen=row.actual_disc, data={"висота": row.actual_height}, src=f"disc:{row.id}")
        for row in rows
    ]
