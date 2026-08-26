"""Tests for the /clients routes (app/web.py) — client profile screens.

Follows the direct-call pattern established in tests/test_settings_routes.py:
a real in-memory SQLite DB via StaticPool, a SimpleNamespace fake Request,
and web.templates.TemplateResponse monkeypatched to return its context dict
so GET routes can be asserted on without a real Jinja render.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base
from app.models import Client, Order, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _operator(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Operator", role="оператор")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None):
    session = {} if user_id is None else {"user_id": user_id}
    return SimpleNamespace(session=session, query_params={})


def _order(client_name, **kwargs):
    return Order(source="lab", client_name=client_name, status="нове", **kwargs)


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )


# --- GET /clients ----------------------------------------------------------


def test_get_clients_requires_authentication():
    engine = _database()
    with Session(engine) as db:
        response = web.get_clients(request=_request(None), db=db)
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_clients_lists_clients_with_order_counts():
    """A client seen in real work gets a card automatically, so «Клієнти» and the
    morning handout show the same people — a client with no card had nowhere to
    configure its export folder. Spelling variants still fold into ONE card."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(Client(canonical_name="Литвиненко Олег"))
        db.add(_order("Литвиненко Олег"))
        db.add(_order("литвиненко олег "))  # case/whitespace variant
        db.add(_order("Хтось Інший"))
        db.commit()

        context = web.get_clients(request=_request(operator.id), db=db)

    rows = {row["client"].canonical_name: row for row in context["client_rows"]}
    assert rows["Литвиненко Олег"]["order_count"] == 2   # both spellings, one card
    assert "Хтось Інший" in rows                          # auto-provisioned from orders
    assert rows["Хтось Інший"]["bound_folder"] is None    # not configured yet


def test_get_clients_is_idempotent_and_does_not_duplicate_cards():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order("Кривовид"))
        db.commit()

        first = web.get_clients(request=_request(operator.id), db=db)
        second = web.get_clients(request=_request(operator.id), db=db)

    assert len(first["client_rows"]) == len(second["client_rows"]) == 1
    assert db.query(Client).count() == 1


# --- POST /clients -----------------------------------------------------


def test_create_client_requires_authentication():
    engine = _database()
    with Session(engine) as db:
        response = web.create_client(
            request=_request(None), canonical_name="Вова", phone="", email="", notes="", db=db
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_create_client_requires_non_blank_name():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        response = web.create_client(
            request=_request(operator.id), canonical_name="   ", phone="", email="", notes="", db=db
        )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    with Session(engine) as db:
        assert db.query(Client).count() == 0


def test_create_client_persists_and_redirects_to_card():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        response = web.create_client(
            request=_request(operator.id),
            canonical_name="  Вова  ",
            phone="0501234567",
            email="vova@example.com",
            notes="постійний клієнт",
            db=db,
        )
    assert response.status_code == 303
    with Session(engine) as db:
        client = db.query(Client).one()
        assert client.canonical_name == "Вова"
        assert client.phone == "0501234567"
        assert response.headers["location"] == f"/clients/{client.id}"


# --- GET /clients/{id} -------------------------------------------------


def test_get_client_detail_requires_authentication():
    engine = _database()
    with Session(engine) as db:
        response = web.get_client_detail(request=_request(None), client_id=1, db=db)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_client_detail_404_for_missing_client():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            web.get_client_detail(request=_request(operator.id), client_id=999, db=db)
    assert exc.value.status_code == 404


def test_get_client_detail_aggregates_matched_orders():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        client = Client(canonical_name="Литвиненко Олег")
        db.add(client)
        db.add(_order("Литвиненко Олег", material_color="пмма A2"))
        db.add(_order("Литвиненко Олег "))  # trailing-space variant, same person
        db.add(_order("Хтось Інший"))
        db.commit()

        context = web.get_client_detail(request=_request(operator.id), client_id=client.id, db=db)

    assert context["summary"].total_count == 2


# --- POST /clients/{id} -------------------------------------------------


def test_update_client_requires_authentication():
    engine = _database()
    with Session(engine) as db:
        response = web.update_client(
            request=_request(None), client_id=1, phone="", email="", notes="", db=db
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_update_client_404_for_missing_client():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        with pytest.raises(HTTPException) as exc:
            web.update_client(
                request=_request(operator.id), client_id=999, phone="", email="", notes="", db=db
            )
    assert exc.value.status_code == 404


def test_update_client_saves_contact_info():
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        client = Client(canonical_name="Вова")
        db.add(client)
        db.commit()

        response = web.update_client(
            request=_request(operator.id),
            client_id=client.id,
            phone="0671112233",
            email="new@example.com",
            notes="змінено",
            db=db,
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/clients/{client.id}?saved=1"
    with Session(engine) as db:
        refreshed = db.get(Client, client.id)
        assert refreshed.phone == "0671112233"
        assert refreshed.email == "new@example.com"
        assert refreshed.notes == "змінено"


# --- POST /clients/{id}/folder — прив'язка папки в export -------------------


class TestBindClientFolder:
    """The folder binding lives on the client card so it is set ONCE and every
    later morning handout reads it, instead of the operator re-confirming a fuzzy
    guess each day. Storage is a confirmed ClientNameAlias — exactly what the
    handout matcher already treats as authoritative."""

    def _client(self, db, name="Басараб"):
        client = Client(canonical_name=name)
        db.add(client)
        db.commit()
        return client

    def test_binding_writes_a_confirmed_alias(self):
        from app.models import ClientNameAlias

        engine = _database()
        with Session(engine) as db:
            user = _operator(db)
            client = self._client(db)
            web.bind_client_folder(
                request=_request(user.id), client_id=client.id,
                export_folder_name="  Басараб Лаб  ", db=db,
            )
            alias = db.query(ClientNameAlias).one()
            assert alias.sheet_name == "Басараб"
            assert alias.export_folder_name == "Басараб Лаб"   # trimmed
            assert alias.confirmed is True and alias.confirmed_at is not None

    def test_rebinding_updates_the_same_alias(self):
        from app.models import ClientNameAlias

        engine = _database()
        with Session(engine) as db:
            user = _operator(db)
            client = self._client(db)
            web.bind_client_folder(request=_request(user.id), client_id=client.id,
                                   export_folder_name="Стара", db=db)
            web.bind_client_folder(request=_request(user.id), client_id=client.id,
                                   export_folder_name="Нова", db=db)
            alias = db.query(ClientNameAlias).one()          # not a second row
            assert alias.export_folder_name == "Нова"

    def test_empty_value_unbinds(self):
        from app.models import ClientNameAlias

        engine = _database()
        with Session(engine) as db:
            user = _operator(db)
            client = self._client(db)
            web.bind_client_folder(request=_request(user.id), client_id=client.id,
                                   export_folder_name="Басараб Лаб", db=db)
            web.bind_client_folder(request=_request(user.id), client_id=client.id,
                                   export_folder_name="", db=db)
            assert db.query(ClientNameAlias).count() == 0

    def test_requires_authentication(self):
        engine = _database()
        with Session(engine) as db:
            client = self._client(db)
            response = web.bind_client_folder(
                request=_request(None), client_id=client.id,
                export_folder_name="Басараб", db=db,
            )
        assert isinstance(response, RedirectResponse)

    def test_unknown_client_is_404(self):
        engine = _database()
        with Session(engine) as db:
            user = _operator(db)
            with pytest.raises(HTTPException) as exc:
                web.bind_client_folder(request=_request(user.id), client_id=999,
                                       export_folder_name="X", db=db)
            assert exc.value.status_code == 404
