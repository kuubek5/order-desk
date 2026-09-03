"""Старт застосунку сам доводить базу до поточної версії міграцій.

Бойовий випадок 03.09.26: `app.web.lifespan` робив лише `Base.metadata.create_all`,
а міграції прогонявся тільки Windows-лаунчер. `create_all` створює ВІДСУТНІ
ТАБЛИЦІ, але ніколи не додає колонку в наявну — тому база тихо відставала від
моделей до першої нової колонки, а тоді кожен запит до тієї таблиці падав з
«no such column: machines.agent_token_encrypted», тобто 500 на всьому
застосунку. Розгрібати довелось руками (`stamp 0034` + `upgrade head`).

Ці тести стережуть, щоб такого «руками» більше не було.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.db import Base
import app.models  # noqa: F401 — реєструє таблиці в Base.metadata
from app.schema import ensure_schema


ROOT = Path(__file__).resolve().parent.parent


def _revision(db_path: Path) -> str | None:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()


def _columns(db_path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    finally:
        connection.close()


def _head() -> str:
    from alembic.script import ScriptDirectory

    from app.schema import alembic_config

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _upgrade_to(db_path: Path, revision: str) -> None:
    """Прогнати міграції до вказаної ревізії ОКРЕМИМ процесом.

    Окремим — бо `migrations/env.py` читає DB_PATH з оточення, і підміна
    всередині процесу тесту перетнулась би з рештою набору.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=dict(os.environ, DB_PATH=str(db_path), PYTHONIOENCODING="utf-8"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"alembic upgrade {revision} впав:\n{result.stderr}"


@pytest.fixture(autouse=True)
def _restore_db_path(monkeypatch):
    # ensure_schema навмисно виставляє DB_PATH для alembic — щоб не протекло
    # в інші тести, фіксуємо його через monkeypatch.
    monkeypatch.setenv("DB_PATH", "unused-by-this-test.db")
    yield


def test_fresh_database_is_created_and_stamped(tmp_path):
    """Чиста база: схема з моделей + штамп, щоб НАСТУПНЕ оновлення мігрувало.

    Саме відсутність штампа й робила дрейф можливим: база без alembic_version
    ніколи не отримає жодної міграції.
    """
    db_path = tmp_path / "fresh.db"
    ensure_schema(db_path, tmp_path / "backups")

    assert db_path.is_file()
    assert _revision(db_path) == _head()
    assert "agent_token_encrypted" in _columns(db_path, "machines")


def test_stale_database_is_migrated_to_head(tmp_path):
    """База, що відстала на міграції, доїжджає сама — без ручного втручання."""
    db_path = tmp_path / "stale.db"
    _upgrade_to(db_path, "0033_default_theme_forge")
    assert "agent_token_encrypted" not in _columns(db_path, "machines")

    ensure_schema(db_path, tmp_path / "backups")

    assert _revision(db_path) == _head()
    assert "agent_token_encrypted" in _columns(db_path, "machines")


def test_stale_database_with_tables_create_all_already_made(tmp_path):
    """РІВНО бойовий випадок 03.09.26.

    Штамп відстав на 0033, але таблиці `feedback`/`feedback_images` уже
    фізично є — їх створив `create_all` на старті. Голий `upgrade head` тут
    падає з «table feedback already exists» і застосунок не піднімається.
    """
    db_path = tmp_path / "mixed.db"
    _upgrade_to(db_path, "0033_default_theme_forge")
    # Те, що робив старий старт: дотворити відсутні таблиці повз alembic.
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    assert _revision(db_path) == "0033_default_theme_forge"
    # Колонки в НАЯВНІЙ таблиці create_all не додає — ось і дрейф.
    assert "agent_token_encrypted" not in _columns(db_path, "machines")

    ensure_schema(db_path, tmp_path / "backups")

    assert _revision(db_path) == _head()
    assert "agent_token_encrypted" in _columns(db_path, "machines")


def test_backup_is_taken_before_migrating(tmp_path):
    """Перед міграцією знімається копія — міграції незворотні на місці."""
    db_path = tmp_path / "stale.db"
    backups = tmp_path / "backups"
    _upgrade_to(db_path, "0033_default_theme_forge")

    ensure_schema(db_path, backups)

    assert list(backups.glob("kuubmill_*.db")), "копія перед міграцією не зроблена"


def test_unknown_unversioned_schema_refuses_to_start(tmp_path):
    """Чужу неверсіоновану базу не чіпаємо: краще не стартувати, ніж зіпсувати."""
    db_path = tmp_path / "alien.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError):
        ensure_schema(db_path, tmp_path / "backups")
