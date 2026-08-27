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

    # ПРОДУКТИВНІСТЬ: `export` живе на Synology через SMB, тож КОЖЕН системний
    # виклик — це мережева ходка. Раніше обхід ішов через Path.iterdir() +
    # .is_dir() + .is_file() + os.stat(): для кожного запису окремий запит.
    # На сотнях клієнтських тек це тисячі ходок і 65с у бойовому логу.
    #
    # os.scandir() віддає тип запису й розмір/час РАЗОМ зі списком теки —
    # Windows кладе це в ту саму відповідь, тож entry.is_dir() і entry.stat()
    # не коштують нічого. Лишається по одному запиту на теку замість запиту
    # на кожен запис у ній.
    def _entries(path) -> list:
        try:
            with os.scandir(path) as it:
                return list(it)
        except (PermissionError, OSError):
            return []

    if not root.exists():
        return []

    entries = []

    for client in _entries(root):                       # рівень 1: клієнти
        try:
            if not client.is_dir():
                continue
        except OSError:
            continue

        for batch in _entries(client.path):             # рівень 2: партії
            try:
                if not batch.is_dir():
                    continue
                created_at = datetime.fromtimestamp(batch.stat().st_ctime)
            except (OSError, PermissionError, ValueError, OverflowError):
                # без часу створення партію не відрізнити від сусідньої —
                # пропускаємо цілком, як і раніше
                continue

            for material in _entries(batch.path):       # рівень 3: матеріал+колір
                try:
                    if not material.is_dir():
                        continue
                except OSError:
                    continue

                files_list = []
                for f in _entries(material.path):       # рівень 4: файли
                    try:
                        if f.is_file():
                            files_list.append(f.name)
                    except OSError:
                        continue

                entries.append(
                    ExportEntry(
                        client_folder_name=client.name,
                        batch_folder_name=batch.name,
                        created_at=created_at,
                        material_color_folder_name=material.name,
                        files=files_list,
                        folder_path=Path(material.path),
                    )
                )

    return entries


# ── Кеш обходу дерева ────────────────────────────────────────────────────
# `export` живе на МЕРЕЖЕВОМУ диску, а обхід triрівневого дерева
# (клієнт / партія / матеріал) — це сотні мережевих stat() на кожну теку.
# Екран видачі викликав scan_export_folder на КОЖНЕ відкриття, незалежно від
# обраного дня. У бойовому логу 25.08.26 це дало «GET /handout took 65.281s»,
# і сторінка фактично перестала відкриватись.
#
# Стратегія — stale-while-revalidate: свіжий кеш віддається одразу, протухлий
# теж віддається одразу, а оновлення йде фоновим потоком. Блокує лише найперше
# сканування після старту. Папка змінюється, коли оператор скачує роботи —
# хвилина затримки тут нічого не варта, а 65 секунд очікування вартують зміни.
_CACHE_TTL_SECONDS = 90.0
_cache: dict[str, tuple[float, list[ExportEntry]]] = {}
_cache_lock = threading.Lock()
_refreshing: set[str] = set()


def _refresh_cache(key: str, root: Path) -> None:
    try:
        entries = scan_export_folder(root)
    except Exception:  # noqa: BLE001 — фонове оновлення не має валити застосунок
        logger.exception("Фонове сканування export не вдалося: %s", root)
        return
    finally:
        with _cache_lock:
            _refreshing.discard(key)
    with _cache_lock:
        _cache[key] = (time.monotonic(), entries)


def scan_export_folder_cached(root: Path) -> list[ExportEntry]:
    """scan_export_folder із кешем на `_CACHE_TTL_SECONDS`.

    Окрема функція, а не прапорець у scan_export_folder: сканер лишається
    чистим і передбачуваним для тестів, які створюють теки й одразу чекають
    їх у результаті. Кеш потрібен рівно одному місцю — екрану видачі.
    """
    key = str(root)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            stamped, entries = cached
            if now - stamped < _CACHE_TTL_SECONDS:
                return entries
            # протухло — віддаємо старе, оновлюємо у фоні
            if key not in _refreshing:
                _refreshing.add(key)
                threading.Thread(
                    target=_refresh_cache, args=(key, Path(root)), daemon=True
                ).start()
            return entries
    # перший раз — доводиться дочекатись
    entries = scan_export_folder(root)
    with _cache_lock:
        _cache[key] = (time.monotonic(), entries)
    return entries


def clear_export_cache() -> None:
    """Скинути кеш — після скачування файлів у export, щоб видача побачила їх одразу."""
    with _cache_lock:
        _cache.clear()
