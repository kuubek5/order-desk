"""Fetches unseen mail from the client-orders mailbox into EmailMessage rows.

Manual/CLI-triggered for now (see app/mail_sync_cli.py) — no scheduling yet,
matching how app/sync_cli.py handles the sheet side at this stage.
"""

from pathlib import Path

from imap_tools import AND, MailBox
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mail_parser import guess_fields_from_text
from app.models import Attachment, EmailMessage
from app.settings_store import get_imap_login, get_imap_password

IMAP_HOST = "imap.ukr.net"


def fetch_new_emails(session: Session, attachments_dir: Path) -> int:
    login = get_imap_login(session)
    password = get_imap_password(session)
    if not login or not password:
        raise RuntimeError("IMAP не налаштовано — задайте логін і пароль у Налаштуваннях")

    created = 0
    with MailBox(IMAP_HOST).login(login, password) as mailbox:
        for msg in mailbox.fetch(AND(seen=False)):
            if session.scalar(select(EmailMessage).where(EmailMessage.uid == msg.uid)) is not None:
                continue

            body = msg.text or msg.html or ""
            guesses = guess_fields_from_text(f"{msg.subject or ''}\n{body}")

            email_message = EmailMessage(
                uid=msg.uid,
                from_address=msg.from_,
                subject=msg.subject,
                body_text=body,
                received_at=msg.date,
                status="нове",
                **guesses,
            )
            session.add(email_message)
            session.flush()

            message_dir = attachments_dir / msg.uid
            for att in msg.attachments:
                message_dir.mkdir(parents=True, exist_ok=True)
                dest_path = message_dir / att.filename
                dest_path.write_bytes(att.payload)
                session.add(
                    Attachment(
                        email_message_id=email_message.id,
                        filename=att.filename,
                        saved_path=str(dest_path),
                        size_bytes=len(att.payload),
                    )
                )

            created += 1

    return created
