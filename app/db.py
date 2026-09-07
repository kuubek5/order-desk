from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import DB_PATH

Base = declarative_base()

db_file = Path(DB_PATH).expanduser().resolve()
db_file.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(URL.create("sqlite", database=str(db_file)), echo=False)


# Чи вмикати перевірку зовнішніх ключів на нових зʼєднаннях.
#
# SQLite за замовчуванням НЕ перевіряє FK: осиротілий рядок (коментар роботи,
# якої вже немає) лягає мовчки й спливає через тижні як «робота без наряду» в
# паспорті або як помилка рендера. Вмикаємо — але з одним запобіжником:
# якщо в базі ВЖЕ є висячі посилання, увімкнення перетворило б кожен наступний
# запис на випадковий IntegrityError, тобто зламало б робочий день замість
# того, щоб щось урятувати. Тому старт спершу звіряє базу
# (`app/schema.py::ensure_schema`) і вимикає перевірку для такої інсталяції,
# лишаючи гучний рядок у лозі.
_fk_enforced = True


def set_foreign_key_enforcement(enabled: bool) -> None:
    """Вирішується на старті, ДО першого запиту (див. ensure_schema)."""
    global _fk_enforced
    _fk_enforced = enabled


def foreign_keys_enforced() -> bool:
    return _fk_enforced


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """Keep local UI reads responsive while short writes are in progress."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if _fk_enforced else 'OFF'}")
    finally:
        cursor.close()

SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
