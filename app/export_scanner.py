"""Scanner for the export folder structure — reading physical milled jobs for morning handoff.

Folder structure:
  export/<client_name>/<batch_folder>/<material_color>/<files>

Walks exactly 3 levels deep, collecting ExportEntry objects for each material-color leaf.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os


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

    if not root.exists():
        return []

    entries = []

    # Level 1: client folders
    try:
        level1_items = list(root.iterdir())
    except (PermissionError, OSError):
        return []

    for client_folder in level1_items:
        if not client_folder.is_dir():
            continue

        client_name = client_folder.name

        # Level 2: batch folders (Windows auto-numbered)
        try:
            level2_items = list(client_folder.iterdir())
        except (PermissionError, OSError):
            continue

        for batch_folder in level2_items:
            if not batch_folder.is_dir():
                continue

            batch_name = batch_folder.name

            # Get batch folder's creation timestamp
            try:
                created_ts = os.stat(str(batch_folder)).st_ctime
                created_at = datetime.fromtimestamp(created_ts)
            except (OSError, PermissionError):
                # If we can't stat this batch folder, skip it entirely
                continue

            # Level 3: material-color folders
            try:
                level3_items = list(batch_folder.iterdir())
            except (PermissionError, OSError):
                continue

            for material_folder in level3_items:
                if not material_folder.is_dir():
                    continue

                material_name = material_folder.name

                # Level 4: files in material-color folder
                try:
                    files_list = [f.name for f in material_folder.iterdir() if f.is_file()]
                except (PermissionError, OSError):
                    # If we can't list files in this material folder, skip it
                    continue

                entry = ExportEntry(
                    client_folder_name=client_name,
                    batch_folder_name=batch_name,
                    created_at=created_at,
                    material_color_folder_name=material_name,
                    files=files_list,
                    folder_path=material_folder,
                )
                entries.append(entry)

    return entries
