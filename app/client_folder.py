"""Тека клієнта в export для пошти — ОДНЕ правило (власник 25.09.26).

Досі в системі жили дві незвʼязані «теки клієнта»: картка клієнта
(`ClientNameAlias`, задається в «Клієнти» й годувала лише видачу) і памʼять
відправника (`ClientSenderMemory.export_folder`, за email — годувала пошту).
Пошта на картку не дивилась узагалі, тож прив'язана в картці тека не діяла на
прийняття листа.

Тепер для пошти порядок такий:
  1. тека з КАРТКИ клієнта — головна, єдине місце, де її задають руками;
  2. памʼять відправника — запас, коли в картці теки немає; і лише для ТОГО
     САМОГО клієнта (ім'я збігається з запам'ятованим), щоб тека одного
     клієнта не переходила на іншого, коли оператор змінив ім'я в картці листа;
  3. далі — звичайне нечітке зіставлення назв (`mail_export`).
Тека має існувати на диску — це перевіряє `mail_export` (інакше — крок 3).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ClientNameAlias


def card_folder_for(db: Session, client_name: str | None) -> str | None:
    """Тека, прив'язана в картці клієнта до цього імені (підтверджена
    `ClientNameAlias`). Регістр і крайні пробіли не важать."""
    name = (client_name or "").strip().casefold()
    if not name:
        return None
    # Порівняння в Python, не в SQL: `lower()` SQLite знає лише латиницю, і
    # «люмі-дент» ≠ «Люмі-Дент» у базі. Підтверджених прив'язок — сотні рядків.
    rows = db.execute(
        select(ClientNameAlias.sheet_name, ClientNameAlias.export_folder_name).where(
            ClientNameAlias.confirmed.is_(True)
        )
    ).all()
    for sheet_name, folder in rows:
        if (sheet_name or "").strip().casefold() == name and (folder or "").strip():
            return folder.strip()
    return None


def preferred_client_folder(db: Session, client_name: str | None, sender_hint) -> str | None:
    """Тека, якій віддати перевагу для цього клієнта (див. докстрінг модуля)."""
    card = card_folder_for(db, client_name)
    if card:
        return card
    if sender_hint is not None and getattr(sender_hint, "export_folder", None):
        name = (client_name or "").strip().casefold()
        hint_name = (getattr(sender_hint, "client_name", None) or "").strip().casefold()
        if not name or not hint_name or name == hint_name:
            return sender_hint.export_folder
    return None
