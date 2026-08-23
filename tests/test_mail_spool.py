"""Mail-spool usage report and the manual cleanup's safety rules."""

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.mail_spool import analyze_spool, prune_spool
from app.models import EmailMessage


def _db() -> Session:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _letter(db, uid, status, days_ago=0):
    db.add(EmailMessage(
        uid=uid, status=status,
        received_at=datetime.now() - timedelta(days=days_ago),
    ))
    db.commit()


def _spool_dir(root, uid, *files):
    d = root / uid
    d.mkdir(parents=True, exist_ok=True)
    for name, data in files:
        (d / name).write_bytes(data)
    return d


def test_analyze_counts_size_and_marks_only_safe_dirs(tmp_path):
    with _db() as db:
        # accepted letter — files gone to export already, folder left empty
        _letter(db, "1", "прийнято")
        empty = _spool_dir(tmp_path, "1")
        # pending letter with real files — must NEVER be prunable
        _letter(db, "2", "нове")
        keep = _spool_dir(tmp_path, "2", ("crown.stl", b"X" * 1000))
        # old rejected letter — prunable
        _letter(db, "3", "відхилено", days_ago=90)
        old_rej = _spool_dir(tmp_path, "3", ("junk.pdf", b"Y" * 2000))
        # recently rejected — too fresh, keep
        _letter(db, "4", "відхилено", days_ago=2)
        fresh_rej = _spool_dir(tmp_path, "4", ("maybe.stl", b"Z" * 500))
        # orphan folder — no letter row at all
        orphan = _spool_dir(tmp_path, "999", ("ghost.stl", b"Q" * 300))

        rep = analyze_spool(db, tmp_path)
        assert rep.total_dirs == 5
        assert rep.total_bytes == 1000 + 2000 + 500 + 300
        prunable = set(rep.prunable_dirs)
        assert empty in prunable
        assert old_rej in prunable
        assert orphan in prunable
        assert keep not in prunable
        assert fresh_rej not in prunable


def test_prune_removes_only_marked_dirs(tmp_path):
    with _db() as db:
        _letter(db, "2", "нове")
        keep = _spool_dir(tmp_path, "2", ("crown.stl", b"X" * 10))
        _letter(db, "3", "відхилено", days_ago=90)
        gone = _spool_dir(tmp_path, "3", ("junk.pdf", b"Y" * 20))

        removed, freed = prune_spool(db, tmp_path)
        assert removed == 1
        assert freed == 20
        assert keep.exists()
        assert not gone.exists()


def test_analyze_on_missing_root_is_empty(tmp_path):
    with _db() as db:
        rep = analyze_spool(db, tmp_path / "nope")
        assert (rep.total_bytes, rep.total_dirs, rep.prunable_dirs) == (0, 0, [])


def test_prune_is_idempotent(tmp_path):
    with _db() as db:
        _letter(db, "3", "відхилено", days_ago=90)
        _spool_dir(tmp_path, "3", ("junk.pdf", b"Y" * 20))
        assert prune_spool(db, tmp_path)[0] == 1
        assert prune_spool(db, tmp_path) == (0, 0)
