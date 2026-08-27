import zipfile
from pathlib import Path

import pytest

from app.archive_extract import ArchiveExtractError, extract_archive, is_archive


def _make_zip(path: Path, entries: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_is_archive():
    assert is_archive("work.zip") is True
    assert is_archive("WORK.RAR") is True
    assert is_archive("crown.stl") is False
    assert is_archive("") is False
    assert is_archive(None) is False


def test_extract_zip_writes_files(tmp_path):
    arc = tmp_path / "work.zip"
    _make_zip(arc, {"crown.stl": b"CROWN", "bridge.stl": b"BRIDGE"})
    dest = tmp_path / "spool"
    dest.mkdir()

    written = extract_archive(arc, dest)

    names = sorted(p.name for p in written)
    assert names == ["bridge.stl", "crown.stl"]
    assert (dest / "crown.stl").read_bytes() == b"CROWN"


def test_extract_flattens_subfolders_and_blocks_zip_slip(tmp_path):
    arc = tmp_path / "work.zip"
    _make_zip(arc, {"sub/deep/model.stl": b"A", "../../escape.stl": b"B"})
    dest = tmp_path / "spool"
    dest.mkdir()

    written = extract_archive(arc, dest)

    # Every file lands directly in dest, by basename — nothing escapes.
    for p in written:
        assert p.parent == dest
        assert p.is_relative_to(dest)
    assert {p.name for p in written} == {"model.stl", "escape.stl"}
    assert not (tmp_path / "escape.stl").exists()  # no traversal out of dest


def test_extract_skips_already_attached_names(tmp_path):
    arc = tmp_path / "work.zip"
    _make_zip(arc, {"crown.stl": b"A", "bridge.stl": b"B"})
    dest = tmp_path / "spool"
    dest.mkdir()

    written = extract_archive(arc, dest, existing_names=frozenset({"crown.stl"}))

    assert [p.name for p in written] == ["bridge.stl"]


def test_extract_empty_archive_raises(tmp_path):
    arc = tmp_path / "empty.zip"
    _make_zip(arc, {})
    dest = tmp_path / "spool"
    dest.mkdir()

    with pytest.raises(ArchiveExtractError, match="немає файлів"):
        extract_archive(arc, dest)


def test_extract_unsupported_format_raises(tmp_path):
    arc = tmp_path / "work.7z"
    arc.write_bytes(b"not a real archive")
    dest = tmp_path / "spool"
    dest.mkdir()

    with pytest.raises(ArchiveExtractError, match="непідтримуваний"):
        extract_archive(arc, dest)


def test_extract_corrupt_zip_raises(tmp_path):
    arc = tmp_path / "broken.zip"
    arc.write_bytes(b"PK\x03\x04 garbage not a zip")
    dest = tmp_path / "spool"
    dest.mkdir()

    with pytest.raises(ArchiveExtractError):
        extract_archive(arc, dest)


def test_extract_archive_attachments_replaces_archive_row_with_files(tmp_path):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.models import Attachment, EmailMessage
    from app.mail_reader import extract_archive_attachments

    spool = tmp_path / "spool"
    spool.mkdir()
    arc = spool / "work.zip"
    _make_zip(arc, {"crown.stl": b"CROWN", "bridge.stl": b"BRIDGE"})

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        email = EmailMessage(uid="u1", status="нове")
        db.add(email)
        db.flush()
        db.add(Attachment(
            email_message_id=email.id, filename="work.zip",
            saved_path=str(arc), size_bytes=arc.stat().st_size,
        ))
        db.commit()

        extracted, errors = extract_archive_attachments(db, email)
        db.commit()

        assert extracted == 2
        assert errors == []
        names = sorted(a.filename for a in db.scalars(select(Attachment)))
        assert names == ["bridge.stl", "crown.stl"]  # archive row gone, files in
        assert not arc.exists()  # archive file removed
        assert (spool / "crown.stl").read_bytes() == b"CROWN"


def test_extract_keeps_archive_until_commit_and_survives_rollback(tmp_path):
    """All-or-nothing: the archive file must NOT be deleted before the commit
    lands. On rollback it survives (so a retry can re-extract), and the
    deferred delete must NOT fire on a later, unrelated commit of the same
    session — that is the exact sequence the sync loop runs (rollback one
    letter, commit the next)."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.models import Attachment, EmailMessage
    from app.mail_reader import extract_archive_attachments

    spool = tmp_path / "spool"
    spool.mkdir()
    arc = spool / "work.zip"
    _make_zip(arc, {"crown.stl": b"CROWN"})

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        email = EmailMessage(uid="u1", status="нове")
        db.add(email)
        db.flush()
        db.add(Attachment(
            email_message_id=email.id, filename="work.zip",
            saved_path=str(arc), size_bytes=arc.stat().st_size,
        ))
        db.commit()

        extracted, _ = extract_archive_attachments(db, email)
        assert extracted == 1
        # Before commit: archive still on disk (only the row is pending delete).
        assert arc.exists()

        db.rollback()
        # Rolled back: archive row is back, archive file untouched.
        assert arc.exists()
        names = sorted(a.filename for a in db.scalars(select(Attachment)))
        assert names == ["work.zip"]

        # A later unrelated commit on the same session must NOT delete the
        # archive (the deferred hook was detached on rollback).
        db.add(EmailMessage(uid="u2", status="нове"))
        db.commit()
        assert arc.exists()

        # Now the happy path on the same session: extract + commit → gone.
        extracted, _ = extract_archive_attachments(db, email)
        db.commit()
        assert extracted == 1
        assert not arc.exists()
