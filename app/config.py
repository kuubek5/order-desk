import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

from app.runtime import data_dir, is_frozen


DATA_DIR = data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENV_PATH = DATA_DIR / ".env" if is_frozen() else Path.cwd() / ".env"


def _bootstrap_packaged_secrets() -> None:
    """Load the packaged Windows master key from user-scoped DPAPI storage."""
    if not is_frozen() or os.name != "nt":
        return
    from app.windows_dpapi import load_or_create_master_key

    # Set the process environment before dotenv is loaded. load_dotenv's
    # default override=False ensures a stale plaintext value cannot supersede
    # the DPAPI-protected key in a packaged installation.
    os.environ["DB_ENCRYPTION_KEY"] = load_or_create_master_key(DATA_DIR)


_bootstrap_packaged_secrets()
load_dotenv(ENV_PATH)

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
DB_PATH = os.environ.get("DB_PATH", str(DATA_DIR / "order_desk.db"))
EXPORT_FOLDER_PATH = os.environ.get("EXPORT_FOLDER_PATH", str(DATA_DIR / "export"))
MAIL_ATTACHMENTS_PATH = os.environ.get(
    "MAIL_ATTACHMENTS_PATH", str(DATA_DIR / "mail_attachments")
)
DB_ENCRYPTION_KEY = os.environ["DB_ENCRYPTION_KEY"]
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY") or hashlib.sha256(
    ("order-desk-session:" + DB_ENCRYPTION_KEY).encode()
).hexdigest()
