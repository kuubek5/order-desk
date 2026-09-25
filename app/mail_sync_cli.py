"""One-shot IMAP synchronization for diagnostics and maintenance."""

import sys
from pathlib import Path

from app.settings_store import get_mail_attachments_path
from app.db import Base, engine, get_session
from app.mail_sync_service import sync_mailbox


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    Base.metadata.create_all(engine)

    with get_session() as session:
        count = sync_mailbox(session, Path(get_mail_attachments_path(session)), trigger="manual")

    print(f"Нових листів: {count}")


if __name__ == "__main__":
    main()
