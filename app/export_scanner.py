"""Scanner for the export folder structure — reading physical milled jobs for morning handoff.

Folder structure:
  export/<client_name>/<batch_folder>/<material_color>/<files>

Walks exactly 3 levels deep, collecting ExportEntry objects for each material-color leaf.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


@dataclass
class ExportEntry:
    """Represents a single material-color folder with its files and metadata."""

    client_folder_name: str
    """Level 1: client name folder (may have typos vs. sheet names — fuzzy matching is caller's job)."""

    batch_folder_name: str
    """Level 2: Windows auto-numbered folder name (e.g., 'Новая папка', 'Новая папка (2)').
    The name carries no information, only the filesystem creation timestamp matters."""

    created_at: datetime
    """Level 2 folder's filesystem creation time.
    On Windows, os.stat(path).st_ctime is the genuine creation time.
    On POSIX, st_ctime is metadata-change time, but this project targets Windows (per CLAUDE.md)."""

    material_color_folder_name: str
    """Level 3: material+color folder name (free-text, e.g., 'mono a3', 'pmma a2')."""

    files: list[str]
    """Filenames (just names, not paths) in the material-color folder.
    May be empty if folder exists but has no files (useful signal: files may still be copying)."""

    folder_path: Path
    """Full path to the level-3 (material-color) folder.
    Kept for later use (e.g., building a 'copy path' button), not used by scanning logic."""


def scan_export_folder(root: Path) -> list[ExportEntry]:
    """Scan the export folder tree and return a flat list of ExportEntry objects.

    Walks exactly 3 levels deep: client / batch / material-color.
    Skips silently (does not raise) on:
      - Non-directory entries where a directory is expected
      - Permission errors on a specific subfolder (logs nothing, just skips that branch)
      - Empty material-color folders (still returns them with files=[])

    If root doesn't exist, returns [] without raising.

    Args:
        root: Root export folder path.

    Returns:
        Flat list of ExportEntry, one per material-color leaf folder found.
    """
    root = Path(root)
    return [
        entry
        for name in list_export_client_names(root)
        for entry in scan_export_client(root, name)
    ]


# ПРОДУКТИВНІСТЬ: `export` — це шара Synology через SMB, де кожен системний
# виклик коштує мережеву ходку. Тому тут два окремих входи:
#
#   list_export_client_names — ОДИН scandir кореня. Дешево, і саме цього
#       достатньо для нечіткого зіставлення імен «таблиця ↔ тека».
#   scan_export_client — глибина (партія / матеріал / файли) для ОДНОГО
#       клієнта. Викликається тільки для тих, хто реально на екрані.
#
# Раніше екран видачі обходив УСЕ дерево, хоча показує 10-20 клієнтів із
# сотень. Тепер робота пропорційна показаному, а не вмісту сховища.


def _dir_entries(path) -> list:
    """os.scandir одним запитом. На Windows тип запису й час створення
    приходять РАЗОМ зі списком теки, тож entry.is_dir()/stat() безкоштовні —
    на відміну від Path.iterdir() + .is_dir(), де кожен запис це окрема ходка."""
    try:
        with os.scandir(path) as it:
            return list(it)
    except (PermissionError, OSError):
        return []


def list_export_client_names(root: Path) -> list[str]:
    """Імена тек клієнтів (рівень 1). Один запит до сховища."""
    root = Path(root)
    if not root.exists():
        return []
    names = []
    for client in _dir_entries(root):
        try:
            if client.is_dir():
                names.append(client.name)
        except OSError:
            continue
    return sorted(names)


def scan_export_client(root: Path, client_folder_name: str) -> list[ExportEntry]:
    """Партії/матеріали/файли ОДНОГО клієнта."""
    client_root = Path(root) / client_folder_name
    entries: list[ExportEntry] = []

    for batch in _dir_entries(client_root):
        try:
            if not batch.is_dir():
                continue
            created_at = datetime.fromtimestamp(batch.stat().st_ctime)
        except (OSError, PermissionError, ValueError, OverflowError):
            # без часу створення партію не відрізнити від сусідньої
            continue

        for material in _dir_entries(batch.path):
            try:
                if not material.is_dir():
                    continue
            except OSError:
                continue

            files_list = []
            for f in _dir_entries(material.path):
                try:
                    if f.is_file():
                        files_list.append(f.name)
                except OSError:
                    continue

            entries.append(
                ExportEntry(
                    client_folder_name=client_folder_name,
                    batch_folder_name=batch.name,
                    created_at=created_at,
                    material_color_folder_name=material.name,
                    files=files_list,
                    folder_path=Path(material.path),
                )
            )
    return entries


# ── Кеш ──────────────────────────────────────────────────────────────────
# stale-while-revalidate: свіже віддається одразу, протухле теж віддається
# одразу з фоновим оновленням. Блокує лише найперший запит. TTL 90с — папка
# змінюється, коли оператор приймає лист, і тоді кеш скидають явно.
_CACHE_TTL_SECONDS = 90.0
_cache: dict[tuple, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_refreshing: set[tuple] = set()


def _cached(key: tuple, producer):
    """Спільна механіка кешу для всіх трьох входів сканера."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            stamped, value = hit
            if now - stamped < _CACHE_TTL_SECONDS:
                return value
            if key not in _refreshing:
                _refreshing.add(key)
                threading.Thread(
                    target=_background_refresh, args=(key, producer), daemon=True
                ).start()
            return value            # протухле краще за очікування
    value = producer()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def _background_refresh(key: tuple, producer) -> None:
    try:
        value = producer()
    except Exception:  # noqa: BLE001 — фонове оновлення не валить застосунок
        logger.exception("Фонове сканування export не вдалося: %s", key)
        return
    finally:
        with _cache_lock:
            _refreshing.discard(key)
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


def list_export_client_names_cached(root: Path) -> list[str]:
    return _cached(("names", str(root)), lambda: list_export_client_names(root))


def scan_export_client_cached(root: Path, client_folder_name: str) -> list[ExportEntry]:
    return _cached(
        ("client", str(root), client_folder_name),
        lambda: scan_export_client(root, client_folder_name),
    )


def scan_export_folder_cached(root: Path) -> list[ExportEntry]:
    """Повний обхід із кешем. Лишається для екранів, яким справді потрібне
    все дерево; видача натомість ходить ліниво, по клієнту."""
    return _cached(("full", str(root)), lambda: scan_export_folder(root))


def clear_export_cache() -> None:
    """Скинути кеш — після переміщення файлів у/з export."""
    with _cache_lock:
        _cache.clear()
