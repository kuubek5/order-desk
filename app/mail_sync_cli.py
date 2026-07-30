"""v0.2 — one-shot IMAP fetch: reads unseen mail into EmailMessage rows for
triage. Run manually for now; no scheduling yet.
"""

import sys
from pathlib import Path

from app.db import Base, engine, get_session
from app.mail_reader import fetch_new_emails


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    Base.metadata.create_all(engine)

    with get_session() as session:
        count = fetch_new_emails(session, Path("mail_attachments"))

    print(f"Нових листів: {count}")


if __name__ == "__main__":
    main()
