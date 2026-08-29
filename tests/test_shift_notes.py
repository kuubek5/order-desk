"""Записки передачі зміни: сервісний шар.

Покриває рішення власника, які легко зламати непомітно: одне «Прийняв» на
записку, різна доля двох типів, редагування скидає прийняття, і бейдж, що
не бреше (лічильник рахує рівно те, що показує дошка).
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import ShiftNote, User
from app.services.shift import (
    KIND_ACTION,
    KIND_INFO,
    ShiftNoteError,
    acknowledge,
    create_note,
    edit_note,
    group_by_night,
    history,
    night_label,
    open_note_count,
    open_notes,
    resolve,
)


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, username="op", full_name="Оп"):
    u = User(username=username, password_hash="x", full_name=full_name, role="оператор")
    db.add(u)
    db.commit()
    return u


def _note(db, kind=KIND_INFO, text="піч №2 відкрити о 9:00", author=None, at=None):
    note = create_note(db, kind=kind, text=text, author=author, now=at)
    db.commit()
    return note


# 1 — створення


def test_create_note_records_author_kind_and_text():
    with Session(_database()) as db:
        author = _user(db)
        note = _note(db, kind=KIND_ACTION, text="  верстат 4 стоїть  ", author=author)

        assert note.kind == KIND_ACTION
        assert note.text == "верстат 4 стоїть"  # обрізано з країв
        assert note.author_id == author.id
        assert note.created_at is not None
        assert note.edited_at is None
        assert note.acknowledged_at is None


def test_create_note_rejects_unknown_kind_and_empty_text():
    with Session(_database()) as db:
        author = _user(db)
        with pytest.raises(ShiftNoteError):
            create_note(db, kind="термінова", text="щось", author=author)
        with pytest.raises(ShiftNoteError):
            create_note(db, kind=KIND_INFO, text="   ", author=author)
        db.rollback()
        assert db.query(ShiftNote).count() == 0


def test_create_note_allows_missing_author():
    """author_id nullable: видалення оператора не має забирати з собою те, що
    він передав зміні."""
    with Session(_database()) as db:
        note = _note(db, author=None)
        assert note.author_id is None


# 2 — прийняття одноразове


def test_acknowledge_is_once_first_wins():
    with Session(_database()) as db:
        first = _user(db, username="a", full_name="Вадим")
        second = _user(db, username="b", full_name="Стас")
        note = _note(db)

        assert acknowledge(db, note, user=first, now=datetime(2026, 8, 28, 8, 5)) is True
        db.commit()

        assert acknowledge(db, note, user=second, now=datetime(2026, 8, 28, 9, 0)) is False
        db.commit()

        assert note.acknowledged_by_id == first.id
        assert note.acknowledged_at == datetime(2026, 8, 28, 8, 5)


# 3 — життєвий цикл двох типів


def test_info_note_leaves_board_once_acknowledged():
    with Session(_database()) as db:
        user = _user(db)
        note = _note(db, kind=KIND_INFO)
        assert open_notes(db) == [note]

        acknowledge(db, note, user=user)
        db.commit()
        assert open_notes(db) == []


def test_action_note_stays_until_resolved():
    with Session(_database()) as db:
        user = _user(db)
        note = _note(db, kind=KIND_ACTION, text="забрати цирконій зі складу")

        acknowledge(db, note, user=user)
        db.commit()
        assert open_notes(db) == [note], "прийняття не закриває «потребує дії»"

        assert resolve(db, note, user=user) is True
        db.commit()
        assert open_notes(db) == []

        assert resolve(db, note, user=user) is False, "закриття ідемпотентне"


def test_resolve_rejected_on_info_note():
    with Session(_database()) as db:
        user = _user(db)
        note = _note(db, kind=KIND_INFO)
        with pytest.raises(ShiftNoteError):
            resolve(db, note, user=user)
        assert note.resolved_at is None


# 4 — бейдж не бреше


def test_open_count_matches_open_notes_on_a_mixed_set():
    with Session(_database()) as db:
        user = _user(db)
        fresh_info = _note(db, kind=KIND_INFO, text="1")
        seen_info = _note(db, kind=KIND_INFO, text="2")
        fresh_action = _note(db, kind=KIND_ACTION, text="3")
        seen_action = _note(db, kind=KIND_ACTION, text="4")
        done_action = _note(db, kind=KIND_ACTION, text="5")

        acknowledge(db, seen_info, user=user)
        acknowledge(db, seen_action, user=user)
        acknowledge(db, done_action, user=user)
        resolve(db, done_action, user=user)
        db.commit()

        visible = open_notes(db)
        assert {n.id for n in visible} == {fresh_info.id, fresh_action.id, seen_action.id}
        assert open_note_count(db) == len(visible)
        assert seen_info not in visible and done_action not in visible


# 5 — редагування скидає прийняття


def test_edit_clears_acknowledgement_and_stamps_edited_at():
    with Session(_database()) as db:
        author = _user(db, username="a")
        reader = _user(db, username="b")
        note = _note(db, text="піч 1 о 9:00", author=author)
        acknowledge(db, note, user=reader)
        db.commit()

        edit_note(db, note, text="піч 1 о 10:00", now=datetime(2026, 8, 28, 5, 40))
        db.commit()

        assert note.text == "піч 1 о 10:00"
        assert note.edited_at == datetime(2026, 8, 28, 5, 40)
        assert note.acknowledged_at is None
        assert note.acknowledged_by_id is None
        assert open_notes(db) == [note], "змінена записка знову на дошці"


def test_edit_without_a_real_change_keeps_acknowledgement():
    """Той самий текст (напр. подвійна відправка форми) не має обнуляти чуже
    «Прийняв»."""
    with Session(_database()) as db:
        reader = _user(db)
        note = _note(db, text="піч 1 о 9:00")
        acknowledge(db, note, user=reader)
        db.commit()

        edit_note(db, note, text="  піч 1 о 9:00  ")
        db.commit()

        assert note.acknowledged_by_id == reader.id
        assert note.edited_at is None


def test_edit_keeps_resolution():
    """Справу зроблено — переформулювання тексту її не відкручує."""
    with Session(_database()) as db:
        user = _user(db)
        note = _note(db, kind=KIND_ACTION, text="закрити піч")
        acknowledge(db, note, user=user)
        resolve(db, note, user=user)
        db.commit()

        edit_note(db, note, text="закрити піч 3")
        db.commit()

        assert note.resolved_by_id == user.id
        assert note.resolved_at is not None


def test_edit_rejects_empty_text():
    with Session(_database()) as db:
        note = _note(db, text="є текст")
        with pytest.raises(ShiftNoteError):
            edit_note(db, note, text="   ")
        assert note.text == "є текст"


# 6 — групування по ночах


def test_night_grouping_keeps_one_handover_together_across_midnight():
    with Session(_database()) as db:
        late = _note(db, text="23:50", at=datetime(2026, 8, 27, 23, 50))
        early = _note(db, text="01:10", at=datetime(2026, 8, 28, 1, 10))
        morning = _note(db, text="04:55", at=datetime(2026, 8, 28, 4, 55))

        groups = group_by_night([late, early, morning])
        assert len(groups) == 1
        assert [n.text for n in groups[0][1]] == ["23:50", "01:10", "04:55"]
        assert night_label(groups[0][0]) == "Ніч 27→28.08"


def test_next_evening_starts_a_new_night():
    with Session(_database()) as db:
        first = _note(db, text="ніч 1", at=datetime(2026, 8, 28, 2, 0))
        second = _note(db, text="ніч 2", at=datetime(2026, 8, 28, 22, 0))

        groups = group_by_night([first, second])
        assert len(groups) == 2
        assert night_label(groups[0][0]) == "Ніч 27→28.08"
        assert night_label(groups[1][0]) == "Ніч 28→29.08"


def test_history_reports_truncation_and_groups_newest_first():
    with Session(_database()) as db:
        for hour in (20, 22, 23):
            _note(db, text=str(hour), at=datetime(2026, 8, 27, hour, 0))
        _note(db, text="стара", at=datetime(2026, 8, 20, 21, 0))

        groups, truncated = history(db, limit=10)
        assert truncated is False
        assert [night_label(start) for start, _ in groups] == [
            "Ніч 27→28.08",
            "Ніч 20→21.08",
        ]
        assert [n.text for n in groups[0][1]] == ["23", "22", "20"]

        groups, truncated = history(db, limit=2)
        assert truncated is True
        assert sum(len(notes) for _, notes in groups) == 2
