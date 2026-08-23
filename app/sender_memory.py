"""«Памʼять відправника» — deterministic recurring-client identification.

The one thing a returning client repeats reliably is the address they write
from; names in the body and subjects drift ("Люмі-Дент" / "Люмі Дент" /
"lumident"). So on every accept we remember, per sender, what the operator
actually did — the client name they typed and the export folder the files
went to — and on the next letter from that sender the accept wizard opens with
both pre-filled and a «постійний клієнт» badge.

Two layers, in order:
  1. ClientSenderMemory (this module): exact sender-key hit → name + folder.
  2. The existing fuzzy name→folder match (app/mail_export.py) stays as the
     fallback when the sender is unknown.

Forwarded mail: one forwarder (the lab's admin) relays many different clients,
so the bare from_address would collide. When the body carries a quoted
"From:/Від:" line we fold the ORIGINAL sender into the key; when it doesn't,
the memory stays silent for that sender rather than guessing (a wrong
"постійний клієнт" suggestion is worse than none).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mail_parser import _REPLY_PREFIX_RE, guess_client_from_forward
from app.models import ClientSenderMemory, EmailMessage


def _is_forwarded(email: EmailMessage) -> bool:
    subject = getattr(email, "subject", None)
    return bool(subject) and _REPLY_PREFIX_RE.match(subject) is not None


def sender_key_for(email: EmailMessage) -> str | None:
    """Stable key for this letter's real sender, or None when it can't be
    pinned down (no from_address, or a forward whose body lacks the original
    From: line — then the forwarder's address alone would mislead)."""
    base = (getattr(email, "from_address", None) or "").strip().lower()
    if not base:
        return None
    if _is_forwarded(email):
        original = guess_client_from_forward(getattr(email, "body_text", None))
        if not original:
            return None
        return f"{base}|{original.strip().lower()}"
    return base


@dataclass(frozen=True)
class SenderHint:
    client_name: str
    export_folder: str | None
    orders_count: int
    last_seen_at: datetime


def lookup_sender(db: Session, email: EmailMessage) -> SenderHint | None:
    key = sender_key_for(email)
    if key is None:
        return None
    row = db.scalar(select(ClientSenderMemory).where(ClientSenderMemory.sender_key == key))
    if row is None:
        return None
    return SenderHint(
        client_name=row.client_name,
        export_folder=row.export_folder,
        orders_count=row.orders_count,
        last_seen_at=row.last_seen_at,
    )


def remember_sender(
    db: Session,
    email: EmailMessage,
    client_name: str,
    export_folder: str | None,
    now: datetime | None = None,
) -> ClientSenderMemory | None:
    """Upsert after a successful accept. Always overwrites name/folder with
    what the operator chose THIS time — the latest correction wins, so a
    renamed client or a moved folder self-heals on the next accept."""
    key = sender_key_for(email)
    client_name = (client_name or "").strip()
    if key is None or not client_name:
        return None
    now = now or datetime.now()
    row = db.scalar(select(ClientSenderMemory).where(ClientSenderMemory.sender_key == key))
    if row is None:
        row = ClientSenderMemory(
            sender_key=key,
            client_name=client_name,
            export_folder=export_folder or None,
            orders_count=1,
            last_seen_at=now,
        )
        db.add(row)
    else:
        row.client_name = client_name
        row.export_folder = export_folder or row.export_folder
        row.orders_count = (row.orders_count or 0) + 1
        row.last_seen_at = now
    return row
