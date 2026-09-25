"""Лист «На уточненні» — пауза в CRM (власник 25.09.26).

Клієнт надіслав дубль файлів, не надіслав файлів чи не вказав матеріал: лист
ставлять на паузу, адміністратори дзвонять замовнику, а відповідь приходить
окремим листом — іноді через пів дня. Щоб такий лист не мозолив очі у «Вхідних»,
він живе у власній вкладці.

Лише стан CRM: скринька ukr.net НЕ змінюється (рішення власника).

Один предикат на всі місця, що рахують «нові листи» (`not_on_hold`): вкладка,
її лічильник, лічильник у рейці, віджет на черзі, «Стан системи». Розійдуться —
бейдж казатиме одне, а список показуватиме інше (та сама пастка, що
`open_notes()`/`open_note_count()`, CLAUDE.md §14).
"""

from __future__ import annotations

from app.business_day import business_now
from app.models import EmailMessage

# Ключ → підпис чіпа. Ключ лягає в базу (`hold_reason`), підпис — лише показ.
HOLD_REASONS: dict[str, str] = {
    "no_files": "Немає файлів",
    "dup_files": "Дубль файлів",
    "no_material": "Не вказано матеріал",
    "other": "Інше",
}

# Лист не на паузі. Висить поряд із not_moved/not_gone на кожному запиті «нових».
not_on_hold = EmailMessage.hold_at.is_(None)
on_hold = EmailMessage.hold_at.is_not(None)

_NOTE_MAX = 300


def hold_label(email: EmailMessage) -> str:
    """Людський підпис причини: «Дубль файлів» або текст оператора для «інше»."""
    note = (email.hold_note or "").strip()
    if not email.hold_reason:
        return note or "На уточненні"
    if email.hold_reason == "other":
        return note or HOLD_REASONS["other"]
    label = HOLD_REASONS.get(email.hold_reason, email.hold_reason)
    return f"{label} · {note}" if note else label


def put_on_hold(email: EmailMessage, reason: str, note: str, actor: str) -> str | None:
    """Поставити лист на паузу. Повертає текст відмови або None.

    Причина НЕОБОВʼЯЗКОВА (власник 25.09.26): з меню «Перемістити» лист іде
    на уточнення одним кліком, як у папку, і «Інше» теж шле кліком — без
    тексту. Порожня причина лягає як None, підпис тоді просто «На уточненні»."""
    reason = (reason or "").strip()
    note = (note or "").strip()[:_NOTE_MAX]
    if reason and reason not in HOLD_REASONS:
        return "Невідома причина"
    if email.status != "нове":
        return "На уточнення можна поставити лише необроблений лист"
    # Лист уже на паузі — це ЗМІНА причини (банер картки): час і автор паузи
    # лишаються первісними, інакше «з 09:12 · Рома» перетворювалось би на час
    # і імʼя того, хто лише дописав причину.
    if email.hold_at is None:
        email.hold_at = business_now().replace(tzinfo=None)
        email.hold_by = actor or None
    email.hold_reason = reason or None
    email.hold_note = note or None
    return None


def release_hold(email: EmailMessage) -> None:
    """Зняти паузу — лист повертається у «Вхідні» (або туди, де йому місце за
    рештою позначок: фільтр, «Покинули Вхідні»)."""
    email.hold_at = None
    email.hold_reason = None
    email.hold_note = None
    email.hold_by = None
