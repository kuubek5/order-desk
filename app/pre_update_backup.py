"""Повна копія бази ПЕРЕД встановленням оновлення.

Місячні знімки (app/monthly_backup.py) рятують від «втратили місяць», ручний
експорт (app/backup.py) — від переїзду на новий ПК. Дірка між ними: оновлення.
Інсталятор і міграції схеми правлять ту саму живу базу, і якщо нова версія
зіпсує дані, відкочуватись нема куди — найсвіжіший місячний знімок може бути
тритижневої давнини.

Тому перед кожним запуском інсталятора знімається окрема копія в
``<тека бази>/backups/pre-update/``. Механізм той самий, що й у місячних
знімках — ``VACUUM INTO``: цілісний самодостатній файл бази без зупинки
застосунку, без пофайлової логіки експорту, яка відстає від схеми.

Пароля тут навмисно нема. Копія існує заради відкату НА ЦЬОМУ Ж ПК, де
DPAPI-ключ на місці, тож секрети всередині лишаються читні застосунком і не
треба вигадувати, звідки взяти пароль у момент, коли адмін просто натиснув
«Оновити». Для переїзду на інший ПК далі служить пароль-захищений експорт.

Тримаємо кілька останніх (``KEEP``): відкат буває потрібен не лише на
найсвіжіше оновлення, але й тримати їх вічно нема сенсу — для «давно» є
місячні знімки.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "kuubmill-pre-"
KEEP = 5


def pre_update_dir(db_path: str | Path) -> Path:
    """Копії лежать поруч із робочою базою, як і місячні знімки."""
    return Path(db_path).expanduser().resolve().parent / "backups" / "pre-update"


def _is_snapshot(path: Path) -> bool:
    return path.is_file() and path.name.startswith(SNAPSHOT_PREFIX) and path.suffix == ".db"


def list_pre_update_snapshots(db_path: str | Path) -> list[Path]:
    """Наявні копії, найновіша перша (сортування за часом у назві)."""
    try:
        files = [p for p in pre_update_dir(db_path).iterdir() if _is_snapshot(p)]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name, reverse=True)


def _safe_version(version: str) -> str:
    """Версія йде в імʼя файлу — лишаємо лише те, що точно не зашкодить шляху."""
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "", (version or "").strip())
    # Роздільників тут уже нема, але «..» в імені лякає при читанні теки —
    # згортаємо крапки й обрізаємо краї, щоб назва лишалась схожою на версію.
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("._-")
    return cleaned or "unknown"


def snapshot_before_update(
    engine: Engine,
    db_path: str | Path,
    version: str,
    now: datetime | None = None,
) -> Path:
    """Зняти копію бази перед оновленням на `version`. Повертає шлях до файлу.

    Кидає виняток при збої копіювання — рішення «оновлюватись без копії чи ні»
    ухвалює той, хто викликав, а не цей модуль.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = pre_update_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{SNAPSHOT_PREFIX}{_safe_version(version)}-{stamp}.db"

    tmp = target.with_suffix(".db.tmp")
    # Недороблена спроба не має валити VACUUM INTO — він відмовляється
    # писати в наявний файл.
    tmp.unlink(missing_ok=True)

    # VACUUM INTO не працює всередині транзакції; шлях наш, не користувацький,
    # подвоєння лапки — на випадок екзотичної теки встановлення.
    quoted = str(tmp).replace("'", "''")
    with engine.connect() as conn:
        conn.exec_driver_sql(f"VACUUM INTO '{quoted}'")

    tmp.replace(target)
    logger.info("Копія бази перед оновленням: %s", target.name)

    _prune(folder)
    return target


def _prune(folder: Path) -> None:
    snapshots = sorted((p for p in folder.iterdir() if _is_snapshot(p)), key=lambda p: p.name)
    excess = len(snapshots) - KEEP
    for path in snapshots[: max(0, excess)]:
        try:
            path.unlink()
            logger.info("Прибрано стару копію перед оновленням: %s", path.name)
        except OSError:
            logger.warning("Не вдалося прибрати копію %s", path.name)
