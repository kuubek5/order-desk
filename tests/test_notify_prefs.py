"""Popup-notification preferences: style, placement, which triggers may fire.

The distinction that matters: an UNSET preference falls back to the defaults,
but an explicitly saved EMPTY event list means "the operator turned everything
off" and must NOT silently re-enable the defaults.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData, Headers
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import User
from app.settings_store import (
    DEFAULT_NOTIFY_POSITION,
    DEFAULT_NOTIFY_STYLE,
    NOTIFY_EVENTS,
    get_notify_events,
    get_notify_position,
    get_notify_style,
    set_notify_prefs,
    set_setting,
)


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _user(db: Session, role="оператор") -> User:
    u = User(username="op", password_hash="unused", full_name="Оператор", role=role)
    db.add(u)
    db.commit()
    return u


def _request(user_id, form=None, headers=None):
    async def _form():
        return FormData(form or [])

    return SimpleNamespace(
        session={} if user_id is None else {"user_id": user_id},
        client=SimpleNamespace(host="127.0.0.1"),
        form=_form,
        headers=Headers(headers or {}),
    )


# --- defaults ---------------------------------------------------------------


def test_defaults_when_nothing_saved():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        assert get_notify_style(db) == DEFAULT_NOTIFY_STYLE == "glass"
        assert get_notify_position(db) == DEFAULT_NOTIFY_POSITION == "tc"
        assert get_notify_events(db) == {k for k, _, _, on in NOTIFY_EVENTS if on}


def test_unknown_style_or_position_falls_back():
    """A hand-edited/garbage value must not reach the CSS as an attribute."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_setting(db, "notify_style", "neon")
        set_setting(db, "notify_position", "middle")
        db.commit()
        assert get_notify_style(db) == "glass"
        assert get_notify_position(db) == "tc"


# --- saving -----------------------------------------------------------------


def test_saving_keeps_only_known_events():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_notify_prefs(db, style="card", position="br", events={"offline", "не-існує"})
        db.commit()
        assert get_notify_style(db) == "card"
        assert get_notify_position(db) == "br"
        assert get_notify_events(db) == {"offline"}


def test_turning_everything_off_is_respected_not_reset_to_defaults():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        set_notify_prefs(db, style="glass", position="tc", events=[])
        db.commit()
        assert get_notify_events(db) == set()


# --- route ------------------------------------------------------------------


def test_route_saves_and_answers_toast_for_htmx():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        request = _request(
            user.id,
            form=[("notify_style", "card"), ("notify_position", "bl"),
                  ("notify_events", "offline"), ("notify_events", "new_mail")],
            headers={"HX-Request": "true"},
        )
        response = asyncio.run(web.save_notification_prefs(request=request, db=db))

    assert response.status_code == 204
    assert json.loads(response.headers["HX-Trigger"])["toast"]["kind"] == "success"
    with Session(engine, expire_on_commit=False) as db:
        assert get_notify_style(db) == "card"
        assert get_notify_position(db) == "bl"
        assert get_notify_events(db) == {"offline", "new_mail"}


def test_route_requires_login():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(web.save_notification_prefs(request=_request(None), db=db))
    assert exc.value.status_code == 401


# --- notify_prefs Jinja global ---------------------------------------------


def test_notify_prefs_global_shape():
    """base.html renders these straight into data-attributes."""
    prefs = web.notify_prefs()
    assert set(prefs) == {"style", "position", "events"}
    assert prefs["style"] in {"glass", "card"}
    assert prefs["position"] in {"tc", "tr", "br", "bl"}
    assert isinstance(prefs["events"], list)


# --- state endpoint ---------------------------------------------------------


def test_notify_state_requires_login():
    engine = _database()
    with Session(engine) as db:
        with pytest.raises(HTTPException) as exc:
            web.api_notify_state(request=_request(None), db=db)
    assert exc.value.status_code == 401


def test_notify_state_returns_the_fields_the_client_diffs():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        user = _user(db)
        state = web.api_notify_state(request=_request(user.id), db=db)
    assert set(state) >= {"sheet", "mail", "orders", "mail_pending", "update"}
    assert isinstance(state["orders"], int)
    assert isinstance(state["mail_pending"], int)
