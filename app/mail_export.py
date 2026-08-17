"""Moves accepted mail attachments into the same client/batch/material tree
that the lab's export folder already uses (CLAUDE.md section 4), so the
morning-handout scanner (app/export_scanner.py) treats mail-sourced work the
same as lab drop-offs.

Runs at accept time (not at raw IMAP fetch) so the folder is named from the
operator-confirmed client name/material, not an unreviewed guess.
"""

from datetime import date
import re
import shutil
from pathlib import Path

from app.client_matcher import match_client_name

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_NO_MATERIAL_NAME = "без_матеріалу"


def _batch_base_name(today: date) -> str:
    """The per-drop-off batch folder is named for the download date (dd.mm.yy),
    so the morning handout can orient by date instead of an opaque "нова папка".
    Same-day drop-offs reuse this folder (or a numbered sibling); a new day gets
    a fresh date folder."""
    return today.strftime("%d.%m.%y")


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


def _batch_number_re(base: str) -> re.Pattern:
    return re.compile(r"^" + re.escape(base) + r" \((\d+)\)$")


def _next_batch_folder(client_dir: Path, base: str) -> Path:
    candidate = client_dir / base
    if not candidate.exists():
        return candidate
    n = 2
    while (client_dir / f"{base} ({n})").exists():
        n += 1
    return client_dir / f"{base} ({n})"


def _latest_batch_folder(client_dir: Path, base: str) -> Path | None:
    """The most recently created batch folder for TODAY's date `base`, or None.

    Batches for one day are named "17.08.26", "17.08.26 (2)", ... in strictly
    increasing order (see _next_batch_folder), so the highest number is the most
    recent. Only folders for THIS date are considered — a different day's date
    folder is a separate drop-off and is never reused, and arbitrary folders a
    technician dropped in by hand are ignored.
    """
    number_re = _batch_number_re(base)
    try:
        candidates = [p for p in client_dir.iterdir() if p.is_dir()]
    except (OSError, FileNotFoundError):
        return None

    best: Path | None = None
    best_n = -1
    for p in candidates:
        if p.name == base:
            n = 1
        else:
            match = number_re.match(p.name)
            if not match:
                continue
            n = int(match.group(1))
        if n > best_n:
            best_n = n
            best = p
    return best


def list_client_folders(export_root: Path) -> list[str]:
    """Existing top-level client folder names under the export root, sorted.
    Feeds the accept wizard's "or pick an existing folder" override list."""
    try:
        return sorted(p.name for p in export_root.iterdir() if p.is_dir())
    except (OSError, FileNotFoundError):
        return []


def preview_export_target(
    export_root: Path,
    client_name: str,
    material_color: str,
    client_folder_override: str | None = None,
    material_folder_override: str | None = None,
    today: date | None = None,
) -> dict:
    """Compute where save_attachments_to_export WOULD put this email's files,
    without touching the filesystem — drives the wizard's directory step so the
    operator confirms the path before committing. Mirrors the same resolver /
    batch-reuse / material-folder logic; keep the two in step."""
    base = _batch_base_name(today or date.today())
    override = (client_folder_override or "").strip()
    if override:
        client_folder = sanitize_folder_name(override)
    else:
        client_folder = _resolve_client_folder_name(export_root, client_name)
    client_dir = _contained_child(export_root, client_folder)
    client_folder_existing = client_dir.is_dir()

    material_override = (material_folder_override or "").strip()
    material_folder = sanitize_folder_name(
        material_override or material_color or _NO_MATERIAL_NAME
    )
    latest_batch = _latest_batch_folder(client_dir, base)
    if latest_batch is not None and not (latest_batch / material_folder).exists():
        batch_folder = latest_batch.name
        batch_reused = True
    else:
        batch_folder = _next_batch_folder(client_dir, base).name
        batch_reused = False

    return {
        "client_folder": client_folder,
        "client_folder_existing": client_folder_existing,
        "batch_folder": batch_folder,
        "batch_reused": batch_reused,
        "material_folder": material_folder,
        "rel_path": f"{client_folder}/{batch_folder}/{material_folder}",
    }


def restore_attachments_to_spool(
    attachments_root: Path, uid: str, current_paths: list[Path]
) -> list[Path]:
    """Move accepted files back from export to their original mail-spool folder
    (attachments_root/<uid>/) — the inverse of save_attachments_to_export, used
    when an accepted email is un-accepted. Returns the new spool paths in input
    order (a still-missing source keeps its computed destination so the caller
    can repoint saved_path anyway). Unique-renames on name collision and rolls
    back a partial move, mirroring save_attachments_to_export."""
    if not current_paths:
        return []
    spool_dir = _contained_child(attachments_root, uid)
    spool_dir.mkdir(parents=True, exist_ok=True)
    reserved: set[Path] = set()
    moves = [
        (source, _unique_destination(spool_dir, source.name, reserved))
        for source in current_paths
    ]
    completed: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves:
            if source.is_file():
                shutil.move(str(source), str(destination))
                completed.append((source, destination))
    except Exception:
        for source, destination in reversed(completed):
            try:
                if destination.exists():
                    shutil.move(str(destination), str(source))
            except Exception:
                pass
        raise
    return [destination for _, destination in moves]


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
    export_root: Path,
    client_name: str,
    material_color: str,
    attachment_paths: list[Path],
    client_folder_override: str | None = None,
    material_folder_override: str | None = None,
    today: date | None = None,
) -> list[Path]:
    """Moves each file in attachment_paths into export_root/<client>/<date>/<material>/.

    The batch folder is named for the download date (dd.mm.yy). Same-day
    drop-offs for one client reuse that date folder as long as each new email
    brings a material it doesn't already have — several materials for the same
    client on the same day pile up inside one date folder. A second order for a
    material the day's folder already holds gets a numbered sibling ("17.08.26
    (2)"); a different day gets a fresh date folder. This mirrors how the
    operator physically drops off boxes, and lets the morning handout orient by
    date. The 3-level client/date/material structure keeps the exact depth
    app/export_scanner.py depends on.

    Returns the new paths in the same order as attachment_paths. Raises on
    filesystem errors (permission denied, unreachable network path, ...) —
    callers decide whether that should block acceptance or just be logged.
    """
    if not attachment_paths:
        return []

    base = _batch_base_name(today or date.today())

    # The accept wizard's directory step lets the operator pin an exact client
    # folder (e.g. reuse "Vision Dental" when the fuzzy match would have made a
    # new "Vision"). An explicit override skips the fuzzy resolver but still
    # goes through _contained_child, so a crafted "../" name can't escape the
    # export root. Empty/whitespace override falls back to the auto resolver.
    override = (client_folder_override or "").strip()
    resolved_name = (
        sanitize_folder_name(override)
        if override
        else _resolve_client_folder_name(export_root, client_name)
    )
    client_dir = _contained_child(export_root, resolved_name)
    material_override = (material_folder_override or "").strip()
    material_name = sanitize_folder_name(
        material_override or material_color or _NO_MATERIAL_NAME
    )

    latest_batch = _latest_batch_folder(client_dir, base)
    if latest_batch is not None and not (latest_batch / material_name).exists():
        batch_dir = latest_batch
    else:
        batch_dir = _next_batch_folder(client_dir, base)
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
