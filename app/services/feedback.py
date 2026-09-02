"""Доменна логіка форми зворотного зв'язку: створення звернень, стрічка
«Вхідні», ретрай Telegram-пуша. Без Request/Response — це сервісний шар.

Джерело правди — база. Пуш у Telegram завжди окремий крок і ніколи не блокує
створення: send_feedback повертає помилку рядком, ми пишемо її в telegram_error
й лишаємо звернення на ретрай.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Feedback, User
from app.services import telegram

logger = logging.getLogger(__name__)

KINDS = {"bug", "idea", "question"}
SEVERITIES = {"minor", "annoying", "blocking"}
STATUSES = {"new", "seen", "resolved"}
MAX_TEXT = 4000
MAX_SCREEN = 60
# Скільки разів фоновий ретрай пробує дошле в Telegram, перш ніж здатись. Далі
# звернення лишається в базі (нічого не втрачено), просто без пуша.
MAX_TELEGRAM_ATTEMPTS = 6


class FeedbackError(Exception):
    """Звернення не прийнято: порожній текст, невідомий тип тощо."""


def create_feedback(
    db: Session,
    *,
    kind: str,
    text: str,
    severity: str | None = None,
    screen: str | None = None,
    app_version: str | None = None,
    author: User | None = None,
    now: datetime | None = None,
) -> Feedback:
    """Створити звернення (без commit — транзакція за роутом)."""
    kind = (kind or "").strip()
    if kind not in KINDS:
        raise FeedbackError("Оберіть тип: баг, ідея або питання.")
    text = (text or "").strip()
    if not text:
        raise FeedbackError("Напишіть, будь ласка, кілька слів.")
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]

    severity = (severity or "").strip() or None
    if severity is not None and severity not in SEVERITIES:
        severity = None
    screen = (screen or "").strip() or None
    if screen is not None and len(screen) > MAX_SCREEN:
        screen = screen[:MAX_SCREEN]

    feedback = Feedback(
        kind=kind,
        severity=severity,
        text=text,
        screen=screen,
        app_version=(app_version or None),
        author_id=author.id if author is not None else None,
        created_at=now or datetime.now(),
        status="new",
    )
    db.add(feedback)
    return feedback


def try_push(db: Session, feedback: Feedback, *, now: datetime | None = None) -> bool:
    """Спробувати доставити в Telegram і записати результат у поля telegram_*.

    НЕ комітить — викликач сам вирішує момент commit. Повертає True при успіху.
    Тихо нічого не робить, якщо пуш вимкнено (запис у базі вже є — цього досить).
    """
    if not telegram.push_enabled(db):
        return False
    feedback.telegram_attempts = (feedback.telegram_attempts or 0) + 1
    ok, err = telegram.send_feedback(db, feedback)
    if ok:
        feedback.telegram_sent_at = now or datetime.now()
        feedback.telegram_error = None
    else:
        feedback.telegram_error = (err or "невідома помилка")[:300]
    return ok


def flush_pending_pushes(db: Session, *, now: datetime | None = None) -> int:
    """Дошле звернення, що не долетіли в Telegram (мережа лягла під час
    створення). Повертає скільки відправлено. Викликається фоновим воркером.
    """
    if not telegram.push_enabled(db):
        return 0
    pending = (
        db.execute(
            select(Feedback)
            .options(selectinload(Feedback.images), selectinload(Feedback.author))
            .where(
                Feedback.telegram_sent_at.is_(None),
                Feedback.telegram_attempts < MAX_TELEGRAM_ATTEMPTS,
            )
            .order_by(Feedback.created_at)
        )
        .scalars()
        .all()
    )
    sent = 0
    for feedback in pending:
        if try_push(db, feedback, now=now):
            sent += 1
    if pending:
        db.commit()
    return sent


def list_feedback(
    db: Session, *, status: str | None = None, limit: int = 200
) -> list[Feedback]:
    query = (
        select(Feedback)
        .options(selectinload(Feedback.images), selectinload(Feedback.author))
        .order_by(Feedback.created_at.desc())
        .limit(limit)
    )
    if status in STATUSES:
        query = query.where(Feedback.status == status)
    return list(db.execute(query).scalars().all())


def open_count(db: Session) -> int:
    """Скільки нових (непрочитаних) звернень — для бейджа в рейці."""
    return int(
        db.execute(
            select(func.count()).select_from(Feedback).where(Feedback.status == "new")
        ).scalar_one()
    )


def mark_seen(db: Session, feedback: Feedback, *, now: datetime | None = None) -> None:
    if feedback.status == "new":
        feedback.status = "seen"
        feedback.seen_at = now or datetime.now()


def mark_resolved(
    db: Session, feedback: Feedback, *, now: datetime | None = None
) -> None:
    feedback.status = "resolved"
    feedback.resolved_at = now or datetime.now()


def reopen(db: Session, feedback: Feedback) -> None:
    feedback.status = "new"
    feedback.resolved_at = None
    feedback.seen_at = None
