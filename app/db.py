from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import DB_PATH

Base = declarative_base()

db_file = Path(DB_PATH).expanduser().resolve()
db_file.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(URL.create("sqlite", database=str(db_file)), echo=False)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """Keep local UI reads responsive while short writes are in progress."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
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
