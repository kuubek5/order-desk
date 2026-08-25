"""Turn the «технік змінив роботу» popup on for existing installs.

Revision ID: 0019_enable_sheet_changed_notify
Revises: 0018_add_sheet_change_flag

Enabled triggers are stored as a fixed comma list, so a new trigger defaults to
ON only where nothing was ever saved. Any install whose operator opened the
notification settings once carries the OLD list — and would silently never see
the scrap-prevention popup, which is the one trigger that must not be missed.
This appends the key to a stored list.

Deliberately does NOT touch an install whose list is EMPTY: that means the
operator turned every popup off on purpose, and this migration must not
override that choice — the queue badge still flags the change either way.

The value is stored ENCRYPTED (app_settings.value_encrypted), so this goes
through the app's own get/set helpers rather than raw SQL — they hold the
encrypt/decrypt in one place.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.orm import Session


revision: str = "0019_enable_sheet_changed_notify"
down_revision: str | None = "0018_add_sheet_change_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = "sheet_changed"


def upgrade() -> None:
    from app.settings_store import get_setting, set_setting

    with Session(bind=op.get_bind()) as session:
        current = get_setting(session, "notify_events")
        if current is None:
            return  # never saved — the per-event default (ON) already applies
        current = current.strip()
        if not current:
            return  # every popup switched off on purpose — leave it alone

        keys = [part.strip() for part in current.split(",") if part.strip()]
        if _KEY in keys:
            return
        keys.append(_KEY)
        set_setting(session, "notify_events", ",".join(keys))
        session.commit()


def downgrade() -> None:
    from app.settings_store import get_setting, set_setting

    with Session(bind=op.get_bind()) as session:
        current = get_setting(session, "notify_events")
        if current is None:
            return
        keys = [
            part.strip()
            for part in current.split(",")
            if part.strip() and part.strip() != _KEY
        ]
        set_setting(session, "notify_events", ",".join(keys))
        session.commit()
