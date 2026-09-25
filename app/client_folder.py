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

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Client, ClientNameAlias

_EMAIL_LIKE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ADDRESS_SPLIT = re.compile(r"[\s,;]+")


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


def _sender_address(email) -> str | None:
    """Адреса справжнього відправника. Для пересланого листа — лише адреса з
    цитованого «From:»: адреса того, хто пересилав (адміністратор, що пересилає
    листи багатьох клієнтів), вела б на чужу картку."""
    from app.sender_memory import _is_forwarded
    from app.mail_parser import guess_client_from_forward

    if _is_forwarded(email):
        original = (guess_client_from_forward(getattr(email, "body_text", None)) or "").strip()
        return original.casefold() if _EMAIL_LIKE.match(original) else None
    address = (getattr(email, "from_address", None) or "").strip().casefold()
    return address or None


def client_for_sender(db: Session, email) -> str | None:
    """Імʼя картки клієнта, в контактах якої вписано адресу відправника
    (власник 25.09.26: вписав пошту в картку «Oleksandr» — а лист однаково
    пропонував теку з назвою адреси). В полі може стояти кілька адрес через кому
    чи пробіл. Дві картки з тією самою адресою — не вгадуємо, None."""
    address = _sender_address(email)
    if not address:
        return None
    hits = {
        (name or "").strip()
        for name, emails in db.execute(
            select(Client.canonical_name, Client.email).where(Client.email.is_not(None))
        ).all()
        if address in {a.casefold() for a in _ADDRESS_SPLIT.split(emails or "") if a}
    }
    hits.discard("")
    return hits.pop() if len(hits) == 1 else None


def is_placeholder_name(name: str | None) -> bool:
    """Імʼя, яке насправді адреса: так «Автоскачування» записує відправника до
    першого прийняття (`client_name` = адреса). Імʼям клієнта його не вважаємо —
    з нього виходила б тека `export/ipad.galiy@gmail.com`."""
    return bool(_EMAIL_LIKE.match((name or "").strip()))


def seed_client_name(db: Session, email, sender_hint) -> str:
    """Імʼя клієнта, яким картка листа відкривається. Порядок:
      1. картка клієнта з цією адресою в контактах — головне джерело;
      2. памʼять відправника — якщо там справжнє імʼя, не адреса-заглушка;
      3. показне імʼя відправника (from_name);
      4. здогад із тексту листа.
    Адреса ніколи не стає імʼям: вона лишається крайнім запасом у шаблоні."""
    card = client_for_sender(db, email)
    if card:
        return card
    hint_name = (getattr(sender_hint, "client_name", None) or "").strip()
    if hint_name and not is_placeholder_name(hint_name):
        return hint_name
    if getattr(email, "from_name", None):
        return email.from_name
    return getattr(email, "client_name_guess", None) or ""
