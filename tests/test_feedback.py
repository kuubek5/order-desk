"""Форма зворотного зв'язку: сервісний шар.

Покриває інваріанти, які легко зламати непомітно: джерело правди — база
(запис лягає навіть коли Telegram недосяжний), пуш ніколи не блокує створення,
лічильник «нових» рахує рівно те, що показує рейка, і ретрай добиває чергу.
"""


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import User
from app.services import feedback as fb
from app.services import telegram as tg


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db, username="op"):
    u = User(username=username, password_hash="x", full_name="Оп", role="оператор")
    db.add(u)
    db.commit()
    return u


# 1 — створення й валідація ----------------------------------------------------

def test_create_feedback_persists_with_context():
    engine = _database()
    with Session(engine) as db:
        user = _user(db)
        item = fb.create_feedback(
            db, kind="bug", text="  скрол стрибає  ",
            severity="annoying", screen="/", app_version="0.6.7", author=user,
        )
        db.commit()
        assert item.id is not None
        assert item.text == "скрол стрибає"  # обрізано
        assert item.kind == "bug"
        assert item.severity == "annoying"
        assert item.status == "new"
        assert item.author_id == user.id


def test_unknown_kind_is_rejected():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(fb.FeedbackError):
            fb.create_feedback(db, kind="spam", text="щось")


def test_empty_text_is_rejected():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(fb.FeedbackError):
            fb.create_feedback(db, kind="idea", text="   ")


def test_bad_severity_falls_back_to_none():
    engine = _database()
    with Session(engine) as db:
        item = fb.create_feedback(db, kind="idea", text="ідея", severity="катастрофа")
        db.commit()
        assert item.severity is None


# 2 — стан і лічильник ---------------------------------------------------------

def test_open_count_matches_new_status():
    engine = _database()
    with Session(engine) as db:
        fb.create_feedback(db, kind="bug", text="a")
        fb.create_feedback(db, kind="bug", text="b")
        db.commit()
        assert fb.open_count(db) == 2

        first = fb.list_feedback(db)[-1]
        fb.mark_resolved(db, first)
        db.commit()
        assert fb.open_count(db) == 1


def test_seen_then_resolve_then_reopen():
    engine = _database()
    with Session(engine) as db:
        item = fb.create_feedback(db, kind="question", text="як?")
        db.commit()
        fb.mark_seen(db, item)
        db.commit()
        assert item.status == "seen" and item.seen_at is not None

        fb.mark_resolved(db, item)
        db.commit()
        assert item.status == "resolved" and item.resolved_at is not None

        fb.reopen(db, item)
        db.commit()
        assert item.status == "new" and item.resolved_at is None


def test_list_filters_by_status():
    engine = _database()
    with Session(engine) as db:
        a = fb.create_feedback(db, kind="bug", text="a")
        fb.create_feedback(db, kind="bug", text="b")
        db.commit()
        fb.mark_resolved(db, a)
        db.commit()
        assert len(fb.list_feedback(db, status="new")) == 1
        assert len(fb.list_feedback(db, status="resolved")) == 1
        assert len(fb.list_feedback(db)) == 2


# 3 — Telegram-пуш ніколи не блокує й не бреше --------------------------------

def test_try_push_noop_when_disabled(monkeypatch):
    engine = _database()
    with Session(engine) as db:
        item = fb.create_feedback(db, kind="bug", text="a")
        db.commit()
        monkeypatch.setattr(tg, "push_enabled", lambda _db: False)
        assert fb.try_push(db, item) is False
        assert item.telegram_attempts == 0  # навіть спроби не рахуємо
        assert item.telegram_sent_at is None


def test_try_push_records_success(monkeypatch):
    engine = _database()
    with Session(engine) as db:
        item = fb.create_feedback(db, kind="bug", text="a")
        db.commit()
        monkeypatch.setattr(tg, "push_enabled", lambda _db: True)
        monkeypatch.setattr(tg, "send_feedback", lambda _db, _f: (True, None))
        assert fb.try_push(db, item) is True
        assert item.telegram_sent_at is not None
        assert item.telegram_error is None
        assert item.telegram_attempts == 1


def test_try_push_records_failure_and_retries(monkeypatch):
    engine = _database()
    with Session(engine) as db:
        item = fb.create_feedback(db, kind="bug", text="a")
        db.commit()
        monkeypatch.setattr(tg, "push_enabled", lambda _db: True)
        monkeypatch.setattr(tg, "send_feedback", lambda _db, _f: (False, "мережа: timeout"))
        assert fb.try_push(db, item) is False
        assert item.telegram_sent_at is None
        assert item.telegram_error == "мережа: timeout"
        assert item.telegram_attempts == 1

        # Ретрай бачить це звернення як незакрите й пробує ще раз.
        calls = {"n": 0}

        def _ok(_db, _f):
            calls["n"] += 1
            return True, None

        monkeypatch.setattr(tg, "send_feedback", _ok)
        sent = fb.flush_pending_pushes(db)
        assert sent == 1 and calls["n"] == 1
        assert item.telegram_sent_at is not None


def test_flush_skips_when_push_disabled(monkeypatch):
    engine = _database()
    with Session(engine) as db:
        fb.create_feedback(db, kind="bug", text="a")
        db.commit()
        monkeypatch.setattr(tg, "push_enabled", lambda _db: False)
        assert fb.flush_pending_pushes(db) == 0


# 4 — роутер: гейти й контекст (патерн test_client_routes) ---------------------

from types import SimpleNamespace  # noqa: E402

import app.web as web  # noqa: E402  (реєструє роутери й templates)
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from app.routers import feedback as feedback_router  # noqa: E402


def _admin(db):
    u = User(username="root", password_hash="x", full_name="Адмін", role="адмін")
    db.add(u)
    db.commit()
    return u


def _request(user_id=None):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, query_params={}, headers={},
                           client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture()
def _stub_templates(monkeypatch):
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )


def test_inbox_requires_admin(_stub_templates):
    engine = _database()
    with Session(engine) as db:
        op = _user(db)  # оператор, не адмін
        with pytest.raises(HTTPException) as exc:
            feedback_router.feedback_inbox(request=_request(op.id), db=db)
        assert exc.value.status_code == 403


def test_inbox_redirects_anonymous(_stub_templates):
    engine = _database()
    with Session(engine) as db:
        resp = feedback_router.feedback_inbox(request=_request(None), db=db)
        assert isinstance(resp, RedirectResponse)
        assert resp.headers["location"] == "/login"


def test_inbox_lists_items_for_admin(_stub_templates):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        fb.create_feedback(db, kind="bug", text="перше", author=admin)
        fb.create_feedback(db, kind="idea", text="друге", author=admin)
        db.commit()
        context = feedback_router.feedback_inbox(request=_request(admin.id), db=db)
        assert len(context["items"]) == 2
        assert context["open_count"] == 2


def test_submit_requires_login():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            feedback_router.submit_feedback(request=_request(None), kind="bug", text="a", db=db)
        assert exc.value.status_code == 401


def test_submit_creates_row_and_survives_push_off(monkeypatch):
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        monkeypatch.setattr(tg, "push_enabled", lambda _db: False)
        resp = feedback_router.submit_feedback(
            request=_request(user.id), kind="bug", text="скрол стрибає",
            severity="", screen="/", images=[], db=db,
        )
        assert resp.status_code == 204  # toast_response
        assert fb.open_count(db) == 1


def test_image_serve_404_when_missing():
    engine = _database()
    with Session(engine) as db:
        _user(db)
        with pytest.raises(HTTPException) as exc:
            feedback_router.get_feedback_image(request=_request(1), image_id=999, db=db)
        assert exc.value.status_code == 404


# 5 — реальний Jinja-рендер стрічки (ловить помилки шаблону) -------------------

def test_inbox_list_partial_renders():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        admin = _admin(db)
        fb.create_feedback(db, kind="bug", text="рядок1", severity="blocking",
                           screen="/", app_version="0.6.7", author=admin)
        fb.create_feedback(db, kind="idea", text="рядок2", author=admin)
        db.commit()
        items = fb.list_feedback(db)
        html = web.templates.get_template("_feedback_inbox_list.html").render(
            items=items, status_filter="all"
        )
        assert "рядок1" in html and "рядок2" in html
        assert "#KM-" in html
