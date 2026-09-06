"""Спільні фікстури тестів — одна база в памʼяті замість 48 копій.

**Навіщо.** Половина тестових файлів дослівно повторює той самий блок:

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)

Копія сама по собі не болить, поки не треба змінити ОДНУ спільну деталь
(прапорець зʼєднання, PRAGMA, спосіб реєстрації моделей) — тоді правку доводиться
рознести по 48 файлах, і один-два неминуче лишаться зі старою поведінкою. Саме так
розходяться «однакові» тести: тут працює, там ні.

**Міграція свідомо поступова.** Переписати всі 48 файлів одним махом — велика
непроглядна правка на код, який і є нашою страховкою; помилка в такій правці
робить тест зеленим і порожнім. Тому тут лише фікстури, а на них переведено
кілька найпростіших файлів як зразок. Решта живе зі своїм локальним `_db()` і
працює далі — фікстури нічого не нав'язують.

**Як перевести наступний файл** (по одному, з прогоном після кожного):

1. Прибрати локальний `_db()` / `_database()` і імпорти `create_engine`,
   `StaticPool`, `Base` (а `Session` — лише якщо більше ніде не потрібен).
2. Додати `db_session` (готова сесія) або `db_engine` (коли тест сам відкриває
   кілька сесій на ту саму базу) в аргументи тест-функції.
3. Прогнати саме цей файл. Якщо тест ловив `expire_on_commit` за замовчуванням
   (тобто СПОДІВАВСЯ, що обʼєкт після `commit()` протухне) — не переводити:
   фікстура дає `expire_on_commit=False`, як переважна більшість наявних копій.

Файл із кількома НЕЗАЛЕЖНИМИ базами в одному тесті переводити не варто — фікстура
дає рівно одну; для таких випадків є `make_memory_engine()`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
import app.models  # noqa: F401 — імпорт реєструє ВСІ таблиці в Base.metadata


def make_memory_engine(check_same_thread: bool = False):
    """Порожня база в памʼяті зі створеною схемою.

    `StaticPool` обовʼязковий: без нього кожне нове зʼєднання до `sqlite://`
    отримує ВЛАСНУ порожню базу, і друга сесія не бачить записаного першою.

    `check_same_thread=False` типово ввімкнено, бо частина тестів ганяє синк і
    фонові воркери в окремих потоках (`test_handout_routes`,
    `test_sheet_write_safety`), а sqlite інакше кидає «SQLite objects created in
    a thread…». Для однопотокового тесту прапорець нешкідливий — він лише знімає
    перевірку драйвера, а не додає паралелізму.
    """
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": check_same_thread},
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_engine():
    """Движок на порожню базу — коли тест сам вирішує, скільки сесій відкрити."""
    engine = make_memory_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Session:
    """Готова сесія до порожньої бази — типовий випадок.

    `expire_on_commit=False` повторює те, що вручну прописано в більшості
    наявних копій: інакше після кожного `commit()` звернення до поля робить
    зайвий SELECT, а обʼєкт, узятий до коміту, стає непридатним.
    """
    with Session(db_engine, expire_on_commit=False) as session:
        yield session
