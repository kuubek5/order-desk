"""Mail-spool usage report and the manual cleanup's safety rules."""

from datetime import datetime, timedelta

from app.mail_spool import analyze_spool, prune_spool
from app.models import EmailMessage

# База — спільна фікстура `db_session` з tests/conftest.py (див. її докстрінг:
# локальні копії `create_engine(StaticPool)` переводяться на неї поступово).


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


def test_analyze_counts_size_and_marks_only_safe_dirs(tmp_path, db_session):
    db = db_session
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


def test_prune_removes_only_marked_dirs(tmp_path, db_session):
    db = db_session
    _letter(db, "2", "нове")
    keep = _spool_dir(tmp_path, "2", ("crown.stl", b"X" * 10))
    _letter(db, "3", "відхилено", days_ago=90)
    gone = _spool_dir(tmp_path, "3", ("junk.pdf", b"Y" * 20))

    removed, freed = prune_spool(db, tmp_path)
    assert removed == 1
    assert freed == 20
    assert keep.exists()
    assert not gone.exists()


def test_analyze_on_missing_root_is_empty(tmp_path, db_session):
    rep = analyze_spool(db_session, tmp_path / "nope")
    assert (rep.total_bytes, rep.total_dirs, rep.prunable_dirs) == (0, 0, [])


def test_analyze_keeps_folder_when_uid_shared_across_uid_validity(tmp_path, db_session):
    # UIDVALIDITY changed on the mailbox: two rows now share the same uid
    # (see app/models.py EmailMessage docstring, migration 0045). One is an
    # old rejected letter from the previous namespace; the other is a live
    # «нове» letter under the new namespace. A naive uid-keyed dict would
    # keep whichever row `session.execute` returns last and could mark the
    # folder prunable even though the live letter still needs its files.
    db = db_session
    db.add(EmailMessage(
        uid="5", uid_validity="111", status="відхилено",
        received_at=datetime.now() - timedelta(days=90),
    ))
    db.add(EmailMessage(
        uid="5", uid_validity="222", status="нове",
        received_at=datetime.now(),
    ))
    db.commit()
    live = _spool_dir(tmp_path, "5", ("crown.stl", b"L" * 400))

    rep = analyze_spool(db, tmp_path)
    assert live not in set(rep.prunable_dirs)


def test_prune_is_idempotent(tmp_path, db_session):
    db = db_session
    _letter(db, "3", "відхилено", days_ago=90)
    _spool_dir(tmp_path, "3", ("junk.pdf", b"Y" * 20))
    assert prune_spool(db, tmp_path)[0] == 1
    assert prune_spool(db, tmp_path) == (0, 0)
