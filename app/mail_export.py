"""Moves accepted mail attachments into the same client/batch/material tree
that the lab's export folder already uses (CLAUDE.md section 4), so the
morning-handout scanner (app/export_scanner.py) treats mail-sourced work the
same as lab drop-offs.

Runs at accept time (not at raw IMAP fetch) so the folder is named from the
operator-confirmed client name/material, not an unreviewed guess.
"""

import re
import shutil
from pathlib import Path

from app.client_matcher import match_client_name

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_BATCH_BASE_NAME = "нова папка"
_NO_MATERIAL_NAME = "без_матеріалу"


def sanitize_folder_name(name: str) -> str:
    name = _ILLEGAL_CHARS.sub("_", name).strip()
    # A dot-only component is meaningful to the filesystem even though it
    # contains none of Windows' forbidden filename characters.
    if name in {".", ".."}:
        return "без_імені"
    return name or "без_імені"


def _contained_child(root: Path, name: str) -> Path:
    """Return a sanitized direct child and reject any root escape."""
    resolved_root = root.resolve()
    child = (resolved_root / sanitize_folder_name(name)).resolve()
    if child.parent != resolved_root:
        raise ValueError("небезпечне ім'я папки")
    return child


def _resolve_client_folder_name(export_root: Path, client_name: str) -> str:
    """Reuses an existing client folder if this client already has one.

    A client who sends a second (or fifth) request weeks later rarely types
    their own name identically each time (whitespace, a typo, "Іванов" vs
    the email address the first time) — without this, every retyped variant
    would fork off its own top-level folder and their order history would
    scatter across the export tree instead of accumulating as batches under
    one client. Reuses the same fuzzy-match this codebase already applies
    the other direction (app/client_matcher.py, sheet name -> export folder
    for Ранкова видача) — same threshold, so the two stay consistent about
    what counts as "the same client". Falls back to a freshly sanitized name
    when there's no confident existing match, which is also what creates the
    very first folder for a brand-new client.
    """
    try:
        existing_folders = sorted(p.name for p in export_root.iterdir() if p.is_dir())
    except (OSError, FileNotFoundError):
        existing_folders = []

    match = match_client_name(client_name, existing_folders, known_aliases={})
    if match.matched_folder_name:
        return match.matched_folder_name
    return sanitize_folder_name(client_name)


_BATCH_NUMBER_RE = re.compile(
    r"^" + re.escape(_BATCH_BASE_NAME) + r" \((\d+)\)$"
)


def _next_batch_folder(client_dir: Path) -> Path:
    candidate = client_dir / _BATCH_BASE_NAME
    if not candidate.exists():
        return candidate
    n = 2
    while (client_dir / f"{_BATCH_BASE_NAME} ({n})").exists():
        n += 1
    return client_dir / f"{_BATCH_BASE_NAME} ({n})"


def _latest_batch_folder(client_dir: Path) -> Path | None:
    """Return the client's most recently created batch folder, or None.

    Batches are named "нова папка", "нова папка (2)", "нова папка (3)", ...
    in strictly increasing order (see _next_batch_folder), so the one with
    the highest number is also the most recently created one. Only folders
    matching that naming scheme are considered — this function is about mail
    -export batches specifically, not arbitrary folders a technician may have
    dropped into the client's export directory by hand.
    """
    try:
        candidates = [p for p in client_dir.iterdir() if p.is_dir()]
    except (OSError, FileNotFoundError):
        return None

    best: Path | None = None
    best_n = -1
    for p in candidates:
        if p.name == _BATCH_BASE_NAME:
            n = 1
        else:
            match = _BATCH_NUMBER_RE.match(p.name)
            if not match:
                continue
            n = int(match.group(1))
        if n > best_n:
            best_n = n
            best = p
    return best


def _unique_destination(directory: Path, filename: str, reserved: set[Path]) -> Path:
    candidate = directory / filename
    stem = candidate.stem
    suffix = candidate.suffix
    number = 2
    while candidate.exists() or candidate in reserved:
        candidate = directory / f"{stem} ({number}){suffix}"
        number += 1
    reserved.add(candidate)
    return candidate


def save_attachments_to_export(
    export_root: Path, client_name: str, material_color: str, attachment_paths: list[Path]
) -> list[Path]:
    """Moves each file in attachment_paths into export_root/<client>/<batch>/<material>/.

    A client's batch folder is reused across separate accepted emails as long
    as each new email brings a material the latest batch doesn't already
    have — several different materials for the same client pile up inside
    one batch. Only when the incoming material already exists in that latest
    batch (a genuine second order for the same material) does a fresh batch
    get created, numbered after the existing ones. This mirrors how the
    operator would physically drop off a second box of files: same batch
    while there's room for another material, a new box only once a slot is
    already taken. The 3-level client/batch/material structure itself never
    changes — app/export_scanner.py depends on exactly that depth.

    Returns the new paths in the same order as attachment_paths. Raises on
    filesystem errors (permission denied, unreachable network path, ...) —
    callers decide whether that should block acceptance or just be logged.
    """
    if not attachment_paths:
        return []

    resolved_name = _resolve_client_folder_name(export_root, client_name)
    client_dir = _contained_child(export_root, resolved_name)
    material_name = sanitize_folder_name(material_color or _NO_MATERIAL_NAME)

    latest_batch = _latest_batch_folder(client_dir)
    if latest_batch is not None and not (latest_batch / material_name).exists():
        batch_dir = latest_batch
    else:
        batch_dir = _next_batch_folder(client_dir)
    material_dir = _contained_child(batch_dir, material_name)
    missing = [path for path in attachment_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"вкладення не знайдено: {missing[0]}")

    material_dir.mkdir(parents=True, exist_ok=True)
    reserved: set[Path] = set()
    moves = [
        (source, _unique_destination(material_dir, source.name, reserved))
        for source in attachment_paths
    ]
    completed: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            shutil.move(str(source), str(destination))
            completed.append((source, destination))
    except Exception:
        rollback_errors = []
        for source, destination in reversed(completed):
            try:
                if destination.exists():
                    shutil.move(str(destination), str(source))
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise OSError("помилка перенесення і відкату: " + "; ".join(rollback_errors))
        raise

    return [destination for _, destination in moves]
