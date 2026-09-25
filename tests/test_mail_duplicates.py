"""Однакові файли в листі (власник 25.09.26): клієнт прикріпив два STL окремо і
ті самі два — в архіві. Розпакування мусить пройти (раніше: «в архіві немає
файлів для розпакування»), а картка — сказати, що це дубль, і не позначити копії."""

from __future__ import annotations

import zipfile
from types import SimpleNamespace

from app.mail_duplicates import base_name, find_duplicates


def _att(tmp_path, id_, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return SimpleNamespace(id=id_, filename=name, saved_path=str(path), size_bytes=len(data))


def test_base_name_strips_copy_suffix():
    assert base_name("crown (2).stl") == "crown.stl"
    assert base_name("Crown.STL") == "crown.stl"
    assert base_name("bridge 11-21.stl") == "bridge 11-21.stl"


def test_identical_content_marks_later_copies(tmp_path):
    atts = [
        _att(tmp_path, 1, "crown.stl", b"CROWN"),
        _att(tmp_path, 2, "bridge.stl", b"BRIDGE-XX"),
        _att(tmp_path, 3, "crown (2).stl", b"CROWN"),
        _att(tmp_path, 4, "bridge (2).stl", b"BRIDGE-XX"),
    ]
    report = find_duplicates(atts)
    assert report.copy_of == {3: "crown.stl", 4: "bridge.stl"}
    assert report.name_conflicts == []  # однакові — це «дубль», не конфлікт


def test_same_name_different_content_is_a_conflict_not_a_duplicate(tmp_path):
    atts = [
        _att(tmp_path, 1, "crown.stl", b"OLD-VERSION"),
        _att(tmp_path, 2, "crown (2).stl", b"NEW"),
    ]
    report = find_duplicates(atts)
    assert report.copy_of == {}
    assert report.name_conflicts == ["crown.stl"] and report.conflict_ids == {1, 2}


def test_renamed_copy_is_still_a_duplicate(tmp_path):
    atts = [_att(tmp_path, 1, "a.stl", b"SAME"), _att(tmp_path, 2, "b.stl", b"SAME")]
    assert find_duplicates(atts).copy_of == {2: "a.stl"}


def test_different_files_are_left_alone(tmp_path):
    atts = [_att(tmp_path, 1, "a.stl", b"AAAA"), _att(tmp_path, 2, "b.stl", b"BBBB")]
    assert not find_duplicates(atts)


def test_archive_with_the_same_names_as_loose_attachments_extracts(tmp_path):
    """Бойовий лист: 2 STL окремо + архів з тими самими 2 STL. Раніше всі імена
    в архіві були «зайняті» → «в архіві немає файлів для розпакування»."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.mail_reader import extract_archive_attachments
    from app.models import Attachment, EmailMessage

    spool = tmp_path / "spool"
    spool.mkdir()
    for name, data in (("crown.stl", b"CROWN"), ("bridge.stl", b"BRIDGE")):
        (spool / name).write_bytes(data)
    arc = spool / "work.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("crown.stl", b"CROWN")
        zf.writestr("bridge.stl", b"BRIDGE")

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        email = EmailMessage(uid="u1", status="нове")
        db.add(email)
        db.flush()
        for name in ("crown.stl", "bridge.stl", "work.zip"):
            path = spool / name
            db.add(Attachment(email_message_id=email.id, filename=name,
                              saved_path=str(path), size_bytes=path.stat().st_size))
        db.commit()
        db.refresh(email)

        extracted, errors = extract_archive_attachments(db, email)
        db.commit()
        assert (extracted, errors) == (2, [])
        rows = list(db.scalars(select(Attachment).order_by(Attachment.id)))
        names = [a.filename for a in rows]
        assert "work.zip" not in names
        assert {"crown (2).stl", "bridge (2).stl"} <= set(names)
        # Картка побачить: копії з архіву — дублі вкладених окремо.
        report = find_duplicates(rows)
        assert set(report.copy_of.values()) == {"crown.stl", "bridge.stl"}


def test_card_reopen_does_not_reread_files(tmp_path, monkeypatch):
    """Картка кличе find_duplicates на кожне відкриття; вміст на мережевому
    спулі читається секундами (прод 25.09.26) — той самий набір файлів удруге
    не читаємо."""
    import app.mail_duplicates as md

    atts = [_att(tmp_path, 1, "a.stl", b"SAME"), _att(tmp_path, 2, "b.stl", b"SAME")]
    reads = []
    real = md._digest
    monkeypatch.setattr(md, "_digest", lambda p: reads.append(p) or real(p))
    first = find_duplicates(atts)
    n = len(reads)
    assert n == 2
    assert find_duplicates(atts).copy_of == first.copy_of == {2: "a.stl"}
    assert len(reads) == n
