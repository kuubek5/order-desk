"""Записки передачі зміни: сервісний шар.

Покриває рішення власника, які легко зламати непомітно: одне «Прийняв» на
записку, різна доля двох типів, редагування скидає прийняття, і бейдж, що
не бреше (лічильник рахує рівно те, що показує дошка).
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import ShiftNote, User
from app.routers import queue as queue_router
from app.routers import shift as shift_router
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


# 7 — роути: доступ, дії, і що дошка справді малюється


def _request(user_id=None):
    return SimpleNamespace(session={} if user_id is None else {"user_id": user_id},
                           query_params={}, headers={})


def _stub_templates(monkeypatch):
    """Роут віддає контекст замість рендера — стиль тестів роутів у проєкті.
    Патчиться атрибут ОБ'ЄКТА templates (спільного для web і deps), а не
    змінна в модулі: після розбиття web.py друге стало б тихим no-op."""
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )


def test_shift_screen_requires_login():
    with Session(_database()) as db:
        response = shift_router.get_shift(request=_request(None), db=db)
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_shift_actions_return_401_without_session():
    with Session(_database()) as db:
        note = _note(db)
        for call in (
            lambda: shift_router.ack_shift_note(request=_request(None), note_id=note.id, db=db),
            lambda: shift_router.resolve_shift_note(request=_request(None), note_id=note.id, db=db),
            lambda: shift_router.create_shift_note(request=_request(None), text="x", kind=KIND_INFO, db=db),
        ):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == 401


def test_board_shows_open_notes_and_history_shows_the_rest(monkeypatch):
    _stub_templates(monkeypatch)
    with Session(_database()) as db:
        user = _user(db)
        open_note = _note(db, text="піч 3 о 12:00", at=datetime(2026, 8, 28, 2, 0))
        old = _note(db, text="давня", at=datetime(2026, 8, 20, 23, 0))
        acknowledge(db, old, user=user)
        db.commit()

        context = shift_router.get_shift(request=_request(user.id), db=db)

    assert [n.id for n in context["board"]] == [open_note.id]
    assert [n.text for _, notes in context["history"] for n in notes] == ["давня"]


def test_night_with_an_open_note_stays_whole_on_the_board(monkeypatch):
    """Одна ніч не має розриватись між дошкою і історією: прийняте з тієї ж
    ночі не повинно поїхати вниз, поки ніч ще жива."""
    _stub_templates(monkeypatch)
    with Session(_database()) as db:
        user = _user(db)
        _note(db, text="відкрите", at=datetime(2026, 8, 28, 1, 0))
        seen = _note(db, text="прийняте тієї ж ночі", at=datetime(2026, 8, 27, 23, 0))
        acknowledge(db, seen, user=user)
        db.commit()

        context = shift_router.get_shift(request=_request(user.id), db=db)

    assert context["history"] == []


def test_ack_route_marks_the_note_and_asks_the_page_to_refresh(monkeypatch):
    with Session(_database()) as db:
        user = _user(db)
        note = _note(db)

        response = shift_router.ack_shift_note(request=_request(user.id), note_id=note.id, db=db)

        assert response.status_code == 204
        assert "refresh-shift" in response.headers["HX-Trigger"]
        assert note.acknowledged_by_id == user.id


def test_edit_route_is_author_only():
    with Session(_database()) as db:
        author = _user(db, username="a")
        other = _user(db, username="b")
        note = _note(db, text="моє", author=author)

        response = shift_router.edit_shift_note(
            request=_request(other.id), note_id=note.id, text="чуже", db=db
        )

        assert response.status_code == 204
        assert "refresh-shift" not in response.headers["HX-Trigger"]
        assert note.text == "моє"


def test_board_partial_renders_for_real():
    """Справжній рендер фрагмента: `partial=board` віддає саме його, а не
    сторінку, і всі фільтри/атрибути в шаблоні існують. Контекст-стаб цього
    не спіймав би — він взагалі не вмикає Jinja."""
    with Session(_database()) as db:
        user = _user(db, full_name="Вадим")
        _note(db, kind=KIND_ACTION, text="верстат 4 стоїть", author=user,
              at=datetime(2026, 8, 28, 1, 20))
        old = _note(db, text="давня записка", author=user, at=datetime(2026, 8, 20, 23, 0))
        acknowledge(db, old, user=user)
        db.commit()

        response = shift_router.get_shift(request=_request(user.id), partial="board", db=db)
        html = response.body.decode("utf-8")

    assert "верстат 4 стоїть" in html
    assert "Потребує дії" in html
    assert 'hx-post="/shift/notes' in html
    assert "Ніч 20→21.08" in html, "заголовок минулої ночі рендериться фільтром night_label"


# 8-9 — картка на черзі


def test_queue_context_carries_open_shift_notes(monkeypatch):
    _stub_templates(monkeypatch)
    with Session(_database()) as db:
        user = _user(db)
        open_note = _note(db, text="піч 2 о 9:00")
        seen = _note(db, text="вже прийняте")
        acknowledge(db, seen, user=user)
        db.commit()

        context = queue_router.get_queue(request=_request(user.id), db=db)

    assert [n.id for n in context["shift_open_notes"]] == [open_note.id]


def test_shift_card_route_always_returns_its_wrapper():
    """Порожня картка мусить лишитись у DOM: віддай роут нічого — зникне сам
    елемент, а з ним і його полл, тож нічна записка більше не проступить."""
    with Session(_database()) as db:
        user = _user(db)
        empty = shift_router.get_shift_card(request=_request(user.id), db=db)
        assert 'id="shift-card"' in empty.body.decode("utf-8")

        _note(db, kind=KIND_ACTION, text="верстат 4 стоїть")
        filled = shift_router.get_shift_card(request=_request(user.id), db=db).body.decode("utf-8")

    assert 'id="shift-card"' in filled
    assert "верстат 4 стоїть" in filled
    assert 'hx-get="/shift/card"' in filled

    with Session(_database()) as db, pytest.raises(HTTPException) as exc:
        shift_router.get_shift_card(request=_request(None), db=db)
    assert exc.value.status_code == 401


def test_shift_card_sits_outside_the_polled_queue_rows():
    """Сторож розташування. Картка мусить стояти ВИЩЕ `#queue-rows`: полл
    черги свапає рівно цей елемент кожні 15 секунд, і все, що всередині,
    зникне разом із ним. Дешева сітка проти випадкового перенесення."""
    markup = (Path("app/templates/queue.html")).read_text(encoding="utf-8")
    card = markup.index('_shift_card.html')
    rows = markup.index('_queue_rows.html')
    assert card < rows, "картку пересунули всередину полла черги"
    assert '"worklayout"' in markup and card < markup.index('"worklayout"')


# 10 — слід скріншотів у картці на черзі


def _shot(db, note, n=1, pruned=False):
    """Рядок зображення без файлу: картці потрібні лише id і стан, байти
    віддає окремий роут."""
    from app.models import ShiftNoteImage

    stamp = datetime(2026, 8, 28, 1, 41)
    db.add(ShiftNoteImage(
        note_id=note.id, filename=f"image{n}.png", saved_path=f"2026-08/{note.id}/{n:02d}.png",
        created_at=stamp, pruned_at=stamp if pruned else None,
    ))
    db.commit()


def _card_html(db, user):
    return shift_router.get_shift_card(request=_request(user.id), db=db).body.decode("utf-8")


def test_shift_card_marks_a_note_with_screenshots_and_its_count():
    with Session(_database()) as db:
        user = _user(db)
        with_shots = _note(db, text="01:41 Sec, pmma a2", at=datetime(2026, 8, 28, 1, 41))
        _shot(db, with_shots, 1)
        _shot(db, with_shots, 2)
        _note(db, text="піч 2 о 9:00", at=datetime(2026, 8, 28, 1, 0))
        html = _card_html(db, user)
        first_image_id = with_shots.images[0].id

    assert html.count('class="sh-card-shots"') == 1, "слід лише в записки зі скріншотами"
    assert f'href="/shift#shift-note-{with_shots.id}"' in html
    assert 'title="2 скріншоти"' in html
    assert '<span class="tnum">2</span>' in html
    assert f'src="/shift/images/{first_image_id}"' in html
    assert 'loading="lazy"' in html and 'width="30" height="16"' in html


def test_shift_card_without_screenshots_has_no_marker():
    with Session(_database()) as db:
        user = _user(db)
        _note(db, text="піч 2 о 9:00")
        html = _card_html(db, user)

    assert "sh-card-shots" not in html
    assert "/shift/images/" not in html


def test_shift_card_counts_pruned_screenshots_but_draws_no_thumbnail_for_them():
    """Прибраний через 6 міс. скріншот лишається слідом («тут була картинка»),
    але мініатюри з нього немає: файл уже не віддається, був би 404."""
    with Session(_database()) as db:
        user = _user(db)
        note = _note(db, text="стара записка")
        _shot(db, note, 1, pruned=True)
        html = _card_html(db, user)

    assert 'title="1 скріншот"' in html
    assert "sh-card-shot-thumb" not in html


def test_shift_card_queries_do_not_grow_with_notes():
    """Картка рендериться на кожен полл: число скріншотів береться з тих
    самих `note.images`, що їх `open_notes` тягне одним selectinload, а не
    окремим запитом на записку."""
    from sqlalchemy import event

    def count_queries(notes):
        engine = _database()
        with Session(engine) as db:
            user = _user(db)
            for i in range(notes):
                note = _note(db, text=f"записка {i}", at=datetime(2026, 8, 28, 1, i))
                _shot(db, note, 1)
                _shot(db, note, 2)
            db.expire_all()
            seen = []

            def listener(*args, **kwargs):
                seen.append(1)

            event.listen(engine, "before_cursor_execute", listener)
            try:
                html = _card_html(db, user)
            finally:
                event.remove(engine, "before_cursor_execute", listener)
            assert html.count('class="sh-card-shots"') == min(notes, 3)
            return len(seen)

    assert count_queries(1) == count_queries(5)


def test_board_note_has_the_anchor_the_card_links_to():
    with Session(_database()) as db:
        user = _user(db)
        note = _note(db, text="верстат 4 стоїть", author=user)
        _shot(db, note, 1)
        html = shift_router.get_shift(
            request=_request(user.id), partial="board", db=db
        ).body.decode("utf-8")

    assert f'id="shift-note-{note.id}"' in html
