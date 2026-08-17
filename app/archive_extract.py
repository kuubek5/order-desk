"""Extract ZIP/RAR archive attachments — clients often send the STL work packed
in an archive instead of loose files, and the CAM operator needs the STL loose
(preview, milling) not a .zip.

Security posture (archives are untrusted email content):
  * Every entry is flattened to its BASENAME into the target dir — no zip-slip
    / path traversal can escape the mail-spool folder.
  * Directories inside the archive are skipped; only files are written.
  * A hard cap on the number of entries and on total uncompressed size guards
    against a zip bomb.
  * A failed extraction cleans up whatever it already wrote (all-or-nothing).

RAR needs the `rarfile` package AND an external UnRAR/7z binary on the machine
(rarfile shells out to it). When that's missing, RAR extraction raises a clear
ArchiveExtractError; ZIP always works (stdlib).
"""

from pathlib import Path
import zipfile

from app.mail_reader import safe_attachment_filename, unique_destination

_ARCHIVE_SUFFIXES = {".zip", ".rar"}
_MAX_ENTRIES = 2000
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB uncompressed


class ArchiveExtractError(Exception):
    """An archive could not be extracted (corrupt, unsupported, too big, or the
    RAR backend tool is missing)."""


def is_archive(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in _ARCHIVE_SUFFIXES


def _open_archive(archive_path: Path):
    """Return (archive_object, entries) where entries is a list of
    (member_name, uncompressed_size) for FILES only (dirs skipped). Raises
    ArchiveExtractError on an unsupported format or a backend failure."""
    suffix = archive_path.suffix.lower()
    if suffix == ".zip":
        try:
            archive = zipfile.ZipFile(archive_path)
        except Exception as exc:  # noqa: BLE001 — normalized below
            raise ArchiveExtractError(f"пошкоджений ZIP: {exc}") from exc
        entries = [
            (info.filename, info.file_size)
            for info in archive.infolist()
            if not info.is_dir()
        ]
        return archive, entries
    if suffix == ".rar":
        try:
            import rarfile
        except ImportError as exc:
            raise ArchiveExtractError(
                "розпакування RAR недоступне: не встановлено бібліотеку rarfile"
            ) from exc
        try:
            archive = rarfile.RarFile(archive_path)
            entries = [
                (info.filename, info.file_size)
                for info in archive.infolist()
                if not info.isdir()
            ]
        except rarfile.RarCannotExec as exc:
            raise ArchiveExtractError(
                "розпакування RAR недоступне: не знайдено UnRAR на цьому комп'ютері"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ArchiveExtractError(f"пошкоджений RAR: {exc}") from exc
        return archive, entries
    raise ArchiveExtractError(f"непідтримуваний формат архіву: {suffix or '—'}")


def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    existing_names: frozenset[str] = frozenset(),
) -> list[Path]:
    """Extract every file in `archive_path` into `dest_dir`, flattened to safe
    basenames, and return the written paths. Skips an entry whose sanitized name
    is already attached (``existing_names``) so a repeat extraction doesn't pile
    up duplicates. Raises ArchiveExtractError on any failure (and cleans up)."""
    archive, entries = _open_archive(archive_path)
    if len(entries) > _MAX_ENTRIES:
        archive.close()
        raise ArchiveExtractError("в архіві забагато файлів")

    written: list[Path] = []
    total = 0
    try:
        for member_name, size in entries:
            total += size or 0
            if total > _MAX_TOTAL_BYTES:
                raise ArchiveExtractError("розпакований архів завеликий")
            base = Path(str(member_name).replace("\\", "/")).name
            safe = safe_attachment_filename(base, 1, "file")
            if safe in existing_names:
                continue
            destination = unique_destination(dest_dir, safe)
            with archive.open(member_name) as source, open(destination, "wb") as fh:
                fh.write(source.read())
            written.append(destination)
    except ArchiveExtractError:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 — normalized, with cleanup
        for path in written:
            path.unlink(missing_ok=True)
        raise ArchiveExtractError(f"не вдалося розпакувати архів: {exc}") from exc
    finally:
        archive.close()

    if not written:
        raise ArchiveExtractError("в архіві немає файлів для розпакування")
    return written
