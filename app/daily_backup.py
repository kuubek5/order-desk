"""Щоденний знімок бази — щоб втрата даних при аварії ПК була днями, а не місяцем.

Знайдено навчанням DR 07.10.26: автоматичні знімки були ЛИШЕ місячні (стан на
початок місяця) і «перед оновленням». Помирає ПК у кінці місяця — і без дзеркала
зникає все, чого нема в Google Таблиці: користувачі, історія статусів,
коментарі, пошта, налаштування, брак і переробки.

Правила:
* один файл на календарний день, `backups/daily/kuubmill-daily-YYYY-MM-DD.db`;
* файл дня ОНОВЛЮЄТЬСЯ, якщо старший за `REFRESH_AFTER` — день тримає стан
  не гірше ніж пів доби тому, а не «перший тік зранку»;
* знімок знімається `VACUUM INTO` (WAL враховано), у `.tmp`, ПЕРЕВІРЯЄТЬСЯ
  `quick_check` і лише тоді переїжджає на місце; зіпсований не лишається;
* зберігаємо `KEEP` останніх днів.
Дзеркало на інший диск — `backup_mirror.mirror_snapshot(subdir="daily")` (кличе воркер).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.engine import Engine

from app.snapshot_tools import verify_integrity

logger = logging.getLogger(__name__)

KEEP = 14
REFRESH_AFTER_SECONDS = 5 * 60 * 60
PREFIX = "kuubmill-daily-"


def daily_dir(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().resolve().parent / "backups" / "daily"


def snapshot_name(day: date) -> str:
    return f"{PREFIX}{day.isoformat()}.db"


def list_daily(db_path: str | Path) -> list[Path]:
    try:
        files = [p for p in daily_dir(db_path).iterdir() if p.is_file() and p.name.startswith(PREFIX) and p.suffix == ".db"]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name, reverse=True)


def ensure_daily_snapshot(
    engine: Engine, db_path: str | Path, today: date | None = None, now: float | None = None
) -> Path | None:
    """Зняти (або оновити) знімок сьогоднішнього дня. None — свіжий уже є.

    Кидає виняток, якщо знімок не вдався або не пройшов перевірку — воркер
    логує й пробує на наступному тіку; половинчастого файлу після себе не лишає."""
    today = today or date.today()
    now = time.time() if now is None else now
    folder = daily_dir(db_path)
    target = folder / snapshot_name(today)
    if target.exists() and now - target.stat().st_mtime < REFRESH_AFTER_SECONDS:
        return None

    folder.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)  # VACUUM INTO не пише в наявний файл

    quoted = str(tmp).replace("'", "''")
    with engine.connect() as conn:
        conn.exec_driver_sql(f"VACUUM INTO '{quoted}'")
    try:
        verify_integrity(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(target)
    logger.info("Щоденний знімок бази: %s", target.name)
    _prune(folder)
    return target


def _prune(folder: Path) -> None:
    snaps = sorted((p for p in folder.iterdir() if p.is_file() and p.name.startswith(PREFIX) and p.suffix == ".db"),
                   key=lambda p: p.name)
    for path in snaps[: max(0, len(snaps) - KEEP)]:
        try:
            path.unlink()
        except OSError:
            logger.warning("Не вдалося прибрати старий щоденний знімок %s", path.name)


__all__ = ["ensure_daily_snapshot", "list_daily", "daily_dir", "KEEP", "datetime"]
