"""One-shot IMAP synchronization for diagnostics and maintenance."""

import sys
from pathlib import Path

from app.config import MAIL_ATTACHMENTS_PATH
from app.db import Base, engine, get_session
from app.mail_sync_service import sync_mailbox


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    Base.metadata.create_all(engine)

    with get_session() as session:
        count = sync_mailbox(session, Path(MAIL_ATTACHMENTS_PATH), trigger="manual")

    print(f"Нових листів: {count}")


if __name__ == "__main__":
    main()
