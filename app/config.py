import os

from dotenv import load_dotenv

load_dotenv()

GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
DB_PATH = os.environ.get("DB_PATH", "order_desk.db")
SESSION_SECRET_KEY = os.environ["SESSION_SECRET_KEY"]
EXPORT_FOLDER_PATH = os.environ.get("EXPORT_FOLDER_PATH", "export")
DB_ENCRYPTION_KEY = os.environ["DB_ENCRYPTION_KEY"]
