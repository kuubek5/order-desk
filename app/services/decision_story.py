"""«Чому так» — ланцюжок рішень однієї роботи словами, від листа до верстата.

Лише читання журналу рішень (`decisions_store`). Подій немає (робота старша за
журнал) — порожній список, екран так і каже.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import decisions_store as store
from app.material_classifier import match_key
from app.models import Order, OrderEmail, User
from app.services import decisions as d

MAX_EVENTS = 400


@dataclass
class Line:
    at: datetime
    title: str
    detail: str = ""
    who: str = ""
    changed: list[str] = field(default_factory=list)
    """Що оператор зробив інакше, ніж показала картка."""
    kind: str = "system"


def email_ids_of(db: Session, order: Order) -> list[int]:
    """Усі листи роботи: свій і внески зведеної (`mail_merge`)."""
    ids = [order.source_email_id] if order.source_email_id else []
    ids += db.scalars(select(OrderEmail.email_message_id).where(OrderEmail.order_id == order.id)).all()
    return sorted(set(ids))


def story(db: Session, order: Order) -> list[Line]:
    emails = email_ids_of(db, order)
    marks = ",".join("?" * len(emails)) or "NULL"
    rows = store.query(
        # Подія з ІНШОЮ роботою того самого листа (друга кольорова партія,
        # переробка) — не про цю роботу.
        f"SELECT * FROM events WHERE order_id = ? OR (email_id IN ({marks}) AND order_id IS NULL) "
        "ORDER BY at, id LIMIT ?",
        [order.id, *emails, MAX_EVENTS],
    )
    users = {u.id: u.full_name or u.username for u in db.scalars(select(User)).all()}
    lines: list[Line] = []
    card: Optional[dict[str, Any]] = None
    for row in rows:
        if row["step"] == d.STEP_CARD and isinstance(row["proposed"], dict):
            card = row["proposed"]
        line = _line(row, card, users)
        if line is None:
            continue
        # Картку перемальовують після кожного скачування — той самий рядок
        # двічі поспіль нічого не каже.
        if lines and (lines[-1].title, lines[-1].detail, lines[-1].kind) == (line.title, line.detail, line.kind):
            continue
        lines.append(line)
    return lines


def _line(row: dict[str, Any], card: Optional[dict[str, Any]], users: dict[int, str]) -> Optional[Line]:
    data = row["data"] if isinstance(row["data"], dict) else {}
    proposed = row["proposed"] if isinstance(row["proposed"], dict) else {}
    chosen = row["chosen"] if isinstance(row["chosen"], dict) else {}
    line = Line(
        at=datetime.fromisoformat(row["at"]), title="", who=users.get(row["user_id"]) or row["actor"],
        kind="human" if row["actor"] == d.ACTOR_OPERATOR else "sheet" if row["actor"] == d.ACTOR_SHEET else "system",
    )
    step = row["step"]
    if step == d.STEP_LETTER:
        line.title, line.detail = "Система прочитала лист", _letter_text(data)
    elif step == d.STEP_CARD:
        line.title, line.detail = "Картка запропонувала", _card_text(proposed, data)
        line.who = d.ACTOR_SYSTEM
    elif step == d.STEP_DISC:
        best = proposed.get("best") if isinstance(proposed.get("best"), dict) else None
        line.title = "Підказка диска"
        line.detail = (
            f"{best.get('disc')}" + (" · впритул" if best.get("tight") else "") if best
            else str(proposed.get("new_disc") or proposed.get("note") or "нічого не підійшло")
        )
    elif step == d.STEP_ACCEPT:
        line.title = "Прийнято" + (" у Конвеєрі" if data.get("режим") == "conveyor" else "")
        line.detail = _values(chosen)
        line.changed = _changes(card, chosen)
    elif step == d.STEP_FATE:
        line.title, line.detail = "Лист", str(row["chosen"] or "")
    elif step == d.STEP_ORDER_NEW:
        line.title = "Робота з'явилась у черзі"
        line.detail = _values(chosen)
    elif step == d.STEP_FIELD:
        line.title = f"Змінено: {row['rule']}"
        line.detail = f"{row['proposed'] or '—'} → {row['chosen'] or '—'}"
    elif step == d.STEP_REWORK:
        line.title = "Переробка"
        line.detail = " · ".join(filter(None, [
            f"{data['який']}-й раз" if data.get("який") else "", f"винен: {data['винен']}" if data.get("винен") else "",
        ]))
    elif step == d.STEP_DISC_ACTUAL:
        line.title, line.detail = "На верстаті взяли диск", str(row["chosen"] or "")
        line.who = ""
    else:
        return None
    return line


def _letter_text(data: dict[str, Any]) -> str:
    files = data.get("файли") or {}
    parts = [f"файлів {files.get('файлів', 0)}"]
    if files.get("одиниць"):
        parts.append(f"з назв {files['одиниць']} од.")
    if files.get("повтори"):
        parts.append("у кількох файлах: " + ", ".join(str(t) for t in files["повтори"]))
    if files.get("без_номерів"):
        parts.append(f"без номера зуба {files['без_номерів']}")
    if data.get("кількість_у_листі"):
        parts.append(f"у тексті {data['кількість_у_листі']}")
    parts.append("матеріал впізнано" if data.get("матеріал_вгадано") else "матеріалу не впізнано")
    return " · ".join(parts)


def _card_text(proposed: dict[str, Any], data: dict[str, Any]) -> str:
    text = _values(proposed) or "порожньо"
    source = data.get("кількість_звідки")
    if proposed.get("кількість") and source in ("файли", "лист"):
        text += " (кількість — " + ("з файлів" if source == "файли" else "з листа") + ")"
    if data.get("розбіжність_лист_файли"):
        text += " · у листі інше число"
    return text


def _values(values: dict[str, Any]) -> str:
    parts = [str(values.get("матеріал") or "")]
    if values.get("кількість"):
        parts.append(f"{values['кількість']} од.")
    if values.get("опак"):
        parts.append(f"опак {values['опак']}")
    if values.get("sum3d"):
        parts.append(f"Sum3D {values['sum3d']}")
    return " · ".join(p for p in parts if p)


def _changes(card: Optional[dict[str, Any]], chosen: dict[str, Any]) -> list[str]:
    if card is None:
        return []
    out = []
    for name in ("клієнт", "матеріал", "кількість"):
        was, now = str(card.get(name) or "").strip(), str(chosen.get(name) or "").strip()
        same = match_key(was) == match_key(now) if name == "матеріал" else was == now
        if not same:
            out.append(f"{name}: {was or 'порожньо'} → {now or 'порожньо'}")
    return out
