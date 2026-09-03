"""Приведення схеми бази до поточної версії — ОДНА реалізація на всі входи.

Досі мігрував лише Windows-лаунчер (`windows_launcher._run_migrations`), а
`app.web.lifespan` для решти шляхів запуску (dev-uvicorn, Docker, будь-який
власний ASGI-хост) робив самий `Base.metadata.create_all`. Різниця тиха й
підступна: `create_all` створює ВІДСУТНІ ТАБЛИЦІ, але ніколи не додає колонку
в наявну. Тому база виглядає живою рівно доти, доки чергова міграція не додасть
поле — і тоді кожен SELECT з тієї таблиці падає з «no such column», тобто 500
на всьому застосунку (спіймано наживо 03.09.26 на `machines.agent_token_encrypted`:
міграції 0034-0035 не застосувались, таблиця `feedback` з'явилась сама через
`create_all`, а колонка в `machines` — ні).

Тому тут не «ще один шлях міграцій», а перенесена сюди єдина: лаунчер тепер
кличе цей самий код. Дві копії однієї послідовності (гард → бекап → upgrade)
розійшлись би так само тихо, як розійшлись би два переліки печей — див.
міграцію 0026.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

# Скільки копій бази тримаємо. Копія знімається перед КОЖНОЮ міграцією, тож
# без ротації тека росла б на кожне оновлення.
BACKUP_KEEP = 5


def backup_database(db_file: Path, backup_dir: Path) -> Path | None:
    """Копія бази перед небезпечною дією. None — копіювати нічого."""
    if not db_file.is_file() or db_file.stat().st_size == 0:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"kuubmill_{datetime.now():%Y%m%d_%H%M%S}.db"
    shutil.copy2(db_file, destination)
    # Ротація бачить і копії зі старим префіксом — інакше вони лишились би на
    # диску назавжди, поза лічильником «тримаємо останні п'ять».
    # Сортування за ЧАСОМ, не за іменем: два різні префікси роблять порядок
    # імен безглуздим, і свіжа копія «kuubmill_» опинялась би після старих
    # «order_desk_» — тобто видалялася б першою.
    backups = sorted(
        [*backup_dir.glob("kuubmill_*.db"), *backup_dir.glob("order_desk_*.db")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[BACKUP_KEEP:]:
        old_backup.unlink(missing_ok=True)
    return destination


def alembic_config():
    from alembic.config import Config

    from app.runtime import resource_path

    config = Config(str(resource_path("alembic.ini")))
    config.set_main_option(
        "script_location", str(resource_path("migrations")).replace("%", "%%")
    )
    return config


def _table_names(db_file: Path) -> set[str]:
    # Звичайний connect, НЕ uri=True: на цьому ПК шляхи бувають UNC
    # (\\host\share\...), а в URI-формі хост читається як authority і
    # sqlite падає з «invalid uri authority» (спіймано живим стартом).
    # Файл на цей момент точно існує — режим «лише читання» тут не потрібен.
    connection = sqlite3.connect(str(db_file))
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()


def _matches_models(db_file: Path) -> bool:
    """Чи вже містить база всі таблиці й колонки з `models.py`.

    Потрібно для однієї конкретної ситуації: базу зробив `create_all` (старий
    шлях запуску), тому `alembic_version` у ній немає, але схема при цьому
    цілком сучасна. Гард нижче назвав би її «незнайомою legacy» і відмовився б
    стартувати, хоча насправді її досить проштампувати.
    """
    from app.db import Base

    # Звичайний connect, НЕ uri=True: на цьому ПК шляхи бувають UNC
    # (\\host\share\...), а в URI-формі хост читається як authority і
    # sqlite падає з «invalid uri authority» (спіймано живим стартом).
    # Файл на цей момент точно існує — режим «лише читання» тут не потрібен.
    connection = sqlite3.connect(str(db_file))
    try:
        for name, table in Base.metadata.tables.items():
            quoted = name.replace('"', '""')
            actual = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{quoted}")')
            }
            if not actual:
                return False
            if {column.name for column in table.columns} - actual:
                return False
    finally:
        connection.close()
    return True


def _already_present_error(exc: BaseException) -> bool:
    """Чи означає помилка «те, що міграція створює, вже існує».

    SQLite каже це двома формулюваннями: «table X already exists» для таблиці
    й «duplicate column name: X» для колонки. Будь-яка інша помилка міграції —
    справжня, і ковтати її не можна.
    """
    message = str(exc).lower()
    return "already exists" in message or "duplicate column name" in message


def ensure_schema(db_file: Path, backup_dir: Path) -> None:
    """Довести базу за `db_file` до поточної версії міграцій.

    Порядок свідомо консервативний: спершу класифікуємо базу, і лише потім
    щось із нею робимо. Незнайому неверсіоновану схему НЕ чіпаємо взагалі —
    краще не стартувати, ніж накотити міграції на чужі дані.
    """
    from alembic import command
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import URL, create_engine

    db_file = Path(db_file).expanduser().resolve()
    config = alembic_config()
    # `migrations/env.py` бере шлях з DB_PATH — без цього рядка alembic мігрував
    # би не ту базу, яку сюди передали, а ту, що в оточенні (спіймано перевіркою
    # на копії відсталої бази: бекап зробився, а міграції пішли повз неї).
    os.environ["DB_PATH"] = str(db_file)
    head_revision = ScriptDirectory.from_config(config).get_current_head()

    fresh = not db_file.is_file() or db_file.stat().st_size == 0
    if not fresh:
        fresh = not _table_names(db_file)

    if fresh:
        # Чиста база: створюємо схему з моделей одним махом (швидше за прогін
        # 35 міграцій) і ставимо штамп, щоб НАСТУПНЕ оновлення вже мігрувало.
        # Саме відсутність штампа й робила дрейф можливим.
        from app.db import Base

        db_file.parent.mkdir(parents=True, exist_ok=True)
        # Движок на ПЕРЕДАНИЙ файл, а не `app.db.engine`: у застосунку це та
        # сама база, але функція приймає шлях параметром і мусить його шанувати
        # — інакше вона мовчки працювала б повз аргумент.
        fresh_engine = create_engine(URL.create("sqlite", database=str(db_file)))
        try:
            Base.metadata.create_all(fresh_engine)
        finally:
            fresh_engine.dispose()
        command.stamp(config, "head")
        logger.info("Схему створено з моделей і проштамповано %s", head_revision)
        return

    if "alembic_version" not in _table_names(db_file):
        if not _matches_models(db_file):
            raise RuntimeError(
                "Схема локальної бази несумісна з цією версією. "
                "Запуск зупинено, дані не змінені."
            )
        # База від старого `create_all`: схема сучасна, бракує лише штампа.
        backup = backup_database(db_file, backup_dir)
        if backup:
            logger.info("Копія перед штампуванням: %s", backup)
        command.stamp(config, "head")
        logger.info("Неверсіоновану базу проштамповано %s", head_revision)
        return

    engine = create_engine(URL.create("sqlite", database=str(db_file)))
    try:
        with engine.connect() as connection:
            current_revision = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    if current_revision == head_revision:
        return

    backup = backup_database(db_file, backup_dir)
    if backup:
        logger.info("Копія перед міграцією: %s", backup)

    # Крок за кроком, а не одним `upgrade head`, через реальний стан баз, які
    # роками стартували через `create_all`: штамп у них відстав, але ЧАСТИНА
    # об'єктів уже фізично існує (нову таблицю `create_all` створював сам, а
    # колонку в наявній — ні). Такій базі `upgrade head` падає на першій же
    # міграції з «table … already exists», і застосунок не піднімається взагалі
    # — саме це й довелось розрулювати руками 03.09.26.
    script = ScriptDirectory.from_config(config)
    pending = list(script.iterate_revisions(head_revision, current_revision))
    pending.reverse()
    for revision in pending:
        try:
            command.upgrade(config, revision.revision)
        except Exception as exc:  # noqa: BLE001 — розбираємо конкретний випадок нижче
            if not _already_present_error(exc):
                raise
            # Об'єкт уже на місці (його зробив `create_all`) — лишається
            # проставити штамп. Правда перевіряється не тут, а фінальною
            # звіркою нижче: якщо після всіх кроків схема не збіглася з
            # моделями, ми не стартуємо взагалі.
            logger.warning(
                "Міграція %s: об'єкт уже існує, ставлю лише штамп", revision.revision
            )
            command.stamp(config, revision.revision)

    if not _matches_models(db_file):
        raise RuntimeError(
            "Після міграцій схема бази не збігається з моделями. "
            "Запуск зупинено; копія бази: " + (str(backup) if backup else "не робилась")
        )
    logger.info("Схему оновлено: %s → %s", current_revision, head_revision)
