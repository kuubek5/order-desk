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


# --- M.3: тека спулу за складеним ключем ------------------------------------


def test_folder_name_is_uidvalidity_and_uid():
    """IMAP UID унікальний лише в межах UIDVALIDITY. Тека звалась самим uid,
    тож після перестворення скриньки два РІЗНІ листи ділили одну теку, і
    вкладення одного лягали поруч із вкладеннями іншого — на видачі це
    «двійник, і невідомо, який справжній»."""
    from app.mail_spool import spool_folder_name

    assert spool_folder_name("42", "99887766") == "99887766_42"
    # Скриньки, які не віддають UIDVALIDITY, і рядки до міграції 0045.
    assert spool_folder_name("42", "") == "42"
    assert spool_folder_name("42", None) == "42"


def test_legacy_folder_is_still_owned_by_its_letter():
    """Теки, створені до складеного імені, не перейменовуються (на них
    посилаються збережені шляхи вкладень) — але й нічийними не стають."""
    from app.mail_spool import folder_candidates

    assert folder_candidates("42", "99887766") == ("99887766_42", "42")
    assert folder_candidates("42", "") == ("42",)


def test_prune_does_not_touch_a_legacy_folder_of_a_live_letter(tmp_path, db_session):
    """Найдорожча помилка тут — прибрати теку живого листа: це видалення
    файлів оператора."""
    db = db_session
    db.add(EmailMessage(
        uid="42", uid_validity="99887766", status="нове",
        received_at=datetime.now(),
    ))
    db.commit()
    _spool_dir(tmp_path, "42", ("crown.stl", b"STL"))

    report = analyze_spool(db, tmp_path)

    assert report.prunable_dirs == []


# --- M.5: звіт про спул не рахується на кожному відкритті налаштувань -------


def test_report_is_cached_between_renders(tmp_path, db_session, monkeypatch):
    """`analyze_spool` рахує розмір КОЖНОЇ теки, тобто обходить весь спул зі
    stat() на кожен файл по мережевій шарі. Це робилось на кожному відкритті
    /settings, хоча цифра змінюється хіба після приймання листа чи
    прибирання."""
    from app import mail_spool

    mail_spool.clear_spool_report_cache()
    _letter(db_session, "1", "нове")
    _spool_dir(tmp_path, "1", ("crown.stl", b"X" * 100))

    calls = []
    real = mail_spool.analyze_spool
    monkeypatch.setattr(
        mail_spool, "analyze_spool",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )

    first = mail_spool.analyze_spool_cached(db_session, tmp_path)
    second = mail_spool.analyze_spool_cached(db_session, tmp_path)

    assert calls == [1]
    assert second.total_bytes == first.total_bytes
    mail_spool.clear_spool_report_cache()


def test_cleanup_button_refreshes_the_number(tmp_path, db_session, monkeypatch):
    """Цифра на екрані мусить одразу показати ефект кнопки, а не висіти
    застарілою до кінця TTL."""
    from app import mail_spool

    mail_spool.clear_spool_report_cache()
    _letter(db_session, "3", "відхилено", days_ago=90)
    _spool_dir(tmp_path, "3", ("junk.pdf", b"Y" * 2000))

    before = mail_spool.analyze_spool_cached(db_session, tmp_path)
    assert before.total_bytes > 0

    prune_spool(db_session, tmp_path)
    after = mail_spool.analyze_spool_cached(db_session, tmp_path)

    assert after.total_bytes == 0
    mail_spool.clear_spool_report_cache()
