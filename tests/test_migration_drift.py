"""Сторож на дрейф «модель ↔ міграції».

Усі інші тести будують схему через `Base.metadata.create_all`, тому колонка,
яку додали в models.py і забули в міграції, у тестах невидима — вона вилізе
лише на робочій базі. Цього разу міграцій було чотири поспіль (0028-0031), і
жоден тест їх не проганяв.

Гірше: після появи дзеркала візуальних налаштувань у сесії така поломка стала
ТИХОЮ. `ui_prefs` ковтає виняток і бере вигляд із куки, тож «no such column»
дасть не гучну 500, а мовчазний відкат оформлення — те, що помітять нескоро.
"""

import os
import subprocess
import sys
import sqlite3
import tempfile
from pathlib import Path

from app.models import User


def _alembic_upgrade(db_path: Path) -> None:
    env = dict(os.environ, DB_PATH=str(db_path), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"alembic upgrade впав:\n{result.stderr}"


def test_migrations_build_the_same_users_table_as_the_model():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "drift.db"
        _alembic_upgrade(db_path)

        conn = sqlite3.connect(db_path)
        try:
            migrated = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        finally:
            conn.close()

    declared = {column.name for column in User.__table__.columns}
    missing = declared - migrated
    extra = migrated - declared
    assert not missing, f"є в models.py, немає в міграціях: {sorted(missing)}"
    assert not extra, f"є в міграціях, немає в models.py: {sorted(extra)}"
