import hashlib
import logging
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
def _db_path() -> str:
    """Шлях до бази з переїздом зі старого імені.

    Файл називався order_desk.db. Просто змінити ім'я не можна: встановлений
    застосунок створив би порожню базу поруч зі старою, і оператор побачив би
    чергу без жодної роботи. Тому при першому запуску під новим іменем файл
    переїжджає разом із супутниками -wal і -shm (без них SQLite вважає базу
    пошкодженою). Не вдалося — лишаємось на старому шляху: краще старе ім'я
    з даними, ніж нове й порожнє.
    """
    new = DATA_DIR / "kuubmill.db"
    legacy = DATA_DIR / "order_desk.db"
    if not new.exists() and legacy.exists():
        try:
            for suffix in ("", "-wal", "-shm"):
                src = legacy.with_name(legacy.name + suffix)
                if src.exists():
                    os.replace(src, new.with_name(new.name + suffix))
        except OSError:
            logging.getLogger(__name__).exception(
                "Не вдалося перейменувати базу %s → %s", legacy, new
            )
            return str(legacy)
    return str(new)


DB_PATH = os.environ.get("DB_PATH", _db_path())
EXPORT_FOLDER_PATH = os.environ.get("EXPORT_FOLDER_PATH", str(DATA_DIR / "export"))
MAIL_ATTACHMENTS_PATH = os.environ.get(
    "MAIL_ATTACHMENTS_PATH", str(DATA_DIR / "mail_attachments")
)
# Скріншоти записок передачі зміни. Свідомо НЕ в /static: та тека віддається
# з бандла PyInstaller (лише читання, затирається кожним оновленням) і виведена
# з-під ліцензійного гейту, тобто доступна без сесії — роботам клієнтів там не місце.
SHIFT_IMAGES_PATH = os.environ.get(
    "SHIFT_IMAGES_PATH", str(DATA_DIR / "shift_images")
)
# Скріншоти до звернень зворотного зв'язку. Та сама логіка, що й у скріншотів
# зміни: НЕ в /static (та тека без сесії й затирається оновленням), окрема
# тека даних під ліцензійним гейтом.
FEEDBACK_IMAGES_PATH = os.environ.get(
    "FEEDBACK_IMAGES_PATH", str(DATA_DIR / "feedback_images")
)
# Останній кадр табло кожної печі. Тримаємо ОДИН файл на піч і перезаписуємо:
# історія картинок нікому не потрібна (у базі лежать числа), а тека, що росте
# по кадру кожні кілька секунд, з'їла б диск за тиждень.
FURNACE_FRAMES_PATH = os.environ.get(
    "FURNACE_FRAMES_PATH", str(DATA_DIR / "furnace_frames")
)
MACHINE_FRAMES_PATH = os.environ.get(
    "MACHINE_FRAMES_PATH", str(DATA_DIR / "machine_frames")
)
DB_ENCRYPTION_KEY = os.environ["DB_ENCRYPTION_KEY"]
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY") or hashlib.sha256(
    ("order-desk-session:" + DB_ENCRYPTION_KEY).encode()
).hexdigest()
