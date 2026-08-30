"""Runtime paths for development, Docker, and packaged Windows builds."""

import logging
import os
from pathlib import Path
import sys

logger = logging.getLogger(__name__)

APP_DIR_NAME = "KuubMill"
# Стара назва теки. НЕ перейменовувати разом з рештою: саме на неї дивиться
# міграція, і без неї встановлений застосунок не знайде свою базу.
LEGACY_DIR_NAME = "Order" "Desk"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _env_override(*names: str) -> str | None:
    """Перше задане значення з переліку змінних оточення.

    KUUBMILL_* — поточні імена, ORDER_DESK_* лишаються прочитними: на робочому
    ПК уже стоїть служба зі старими змінними, і мовчазна втрата override
    означала б, що застосунок піде не в ту теку з даними.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _migrate_legacy_dir(new: Path, legacy: Path) -> None:
    """Перенести теку даних зі старої назви, якщо нової ще немає.

    Перейменування застосунку не має коштувати оператору його бази. Тому при
    першому запуску під новим іменем стара тека переїжджає цілком —
    os.replace атомарний у межах одного диска, тож або переїхало все, або
    нічого. Якщо переїзд не вдався, працюємо далі зі СТАРОЮ текою: краще
    старий шлях із даними, ніж новий і порожній.
    """
    if new.exists() or not legacy.exists():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy, new)
        logger.info("Теку даних перенесено: %s → %s", legacy, new)
    except OSError:
        logger.exception("Не вдалося перенести теку даних %s → %s", legacy, new)


def data_dir() -> Path:
    override = _env_override("KUUBMILL_DATA_DIR", "ORDER_DESK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen() and os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        new, legacy = root / APP_DIR_NAME, root / LEGACY_DIR_NAME
        _migrate_legacy_dir(new, legacy)
        return new if new.exists() or not legacy.exists() else legacy
    return Path.cwd()


def resource_path(relative: str) -> Path:
    """Resolve a PyInstaller resource or the same path in the source tree."""
    source_root = Path(__file__).resolve().parents[1]
    bundle_root = Path(getattr(sys, "_MEIPASS", source_root))
    return bundle_root / relative
