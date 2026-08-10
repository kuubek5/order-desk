"""Runtime paths for development, Docker, and packaged Windows builds."""

import os
from pathlib import Path
import sys


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    override = os.environ.get("ORDER_DESK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen() and os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "OrderDesk"
        return Path.home() / "AppData" / "Local" / "OrderDesk"
    return Path.cwd()


def resource_path(relative: str) -> Path:
    """Resolve a PyInstaller resource or the same path in the source tree."""
    source_root = Path(__file__).resolve().parents[1]
    bundle_root = Path(getattr(sys, "_MEIPASS", source_root))
    return bundle_root / relative
