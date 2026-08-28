"""Tests for the search functionality (GET /search).

Follows the direct-call pattern established in tests/test_settings_routes.py:
a real in-memory SQLite DB via StaticPool, a SimpleNamespace fake Request,
and web.templates.TemplateResponse monkeypatched to return its context dict
so GET routes can be asserted on without a real Jinja render.
"""

from types import SimpleNamespace

import pytest
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.routers import queue as queue_router_mod
from app.db import Base
from app.models import Order, User


def _database():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def _operator(db: Session) -> User:
    user = User(username="operator", password_hash="unused", full_name="Operator", role="оператор")
    db.add(user)
    db.commit()
    return user


def _request(user_id: int | None, query_params: dict | None = None):
    session = {} if user_id is None else {"user_id": user_id}
    params = query_params or {}
    return SimpleNamespace(session=session, query_params=params)


def _order(source="lab", client_name=None, **kwargs):
    return Order(source=source, client_name=client_name, status="нове", **kwargs)


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    monkeypatch.setattr(
        web.templates, "TemplateResponse", lambda request, template, context: context
    )


# --- GET /search ---


def test_search_requires_authentication():
    """Unauthenticated request redirects to /login."""
    engine = _database()
    with Session(engine) as db:
        response = queue_router_mod.get_search(request=_request(None), q="test", db=db)
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_search_empty_query_shows_prompt():
    """Empty query shows prompt state, not results."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="Test Client"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="", db=db)

    assert context["query"] == ""
    assert context["results"] == []


def test_search_by_client_name():
    """Search finds orders by client name."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="ПТК Dent Studio", work_order_no="12345"))
        db.add(_order(client_name="Dr. Smile Lab", work_order_no="12346"))
        db.add(_order(client_name="Сміль Дент", source="email"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="Dent", db=db)

    # Should find orders with "Dent" in client_name (first and third)
    assert len(context["results"]) >= 1
    client_names = [o.client_name for o in context["results"]]
    assert any("Dent" in name for name in client_names if name)


def test_search_by_work_order_no():
    """Search finds orders by work order number."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="Client A", work_order_no="12345"))
        db.add(_order(client_name="Client B", work_order_no="54321"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="12345", db=db)

    assert len(context["results"]) == 1
    assert context["results"][0].work_order_no == "12345"


def test_search_by_job_code():
    """Search finds orders by job code."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="Client A", job_code="2026-08-10_00001-001"))
        db.add(_order(client_name="Client B", job_code="2026-08-10_00002-001"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="2026-08-10_00001", db=db)

    assert len(context["results"]) == 1
    assert context["results"][0].job_code == "2026-08-10_00001-001"


def test_search_by_sum3d_id():
    """Search finds orders by Sum3D ID."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="Client A", sum3d_id="proj_123abc"))
        db.add(_order(client_name="Client B", sum3d_id="proj_456def"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="proj_123abc", db=db)

    assert len(context["results"]) == 1
    assert context["results"][0].sum3d_id == "proj_123abc"


def test_search_case_insensitive():
    """Search is case-insensitive."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="Dent Studio", work_order_no="12345"))
        db.add(_order(client_name="dent lab", work_order_no="54321"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="DENT", db=db)

    # Should find both
    assert len(context["results"]) == 2


def test_search_partial_match():
    """Search works with partial string matches."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="ПТК Dent Studio", work_order_no="12345"))
        db.add(_order(client_name="Smile Dent Lab", work_order_no="67890"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="Dent", db=db)

    assert len(context["results"]) == 2


def test_search_no_results():
    """Search with no matching results returns empty list."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="Client A", work_order_no="12345"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="nonexistent_xyz", db=db)

    assert context["results"] == []


def test_search_multiple_fields():
    """Search can match across different fields."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        # Order with query in client_name
        db.add(_order(client_name="Medical Center", work_order_no="11111"))
        # Order with query in work_order_no
        db.add(_order(client_name="Other", work_order_no="22222"))
        # Order with query in job_code
        db.add(_order(client_name="Third", job_code="med_code_001"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="med", db=db)

    # Should find at least the first and third (client_name and job_code)
    assert len(context["results"]) >= 2


def test_search_result_limit():
    """Search results are capped at 100."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        # Create 105 orders with matching query
        for i in range(105):
            db.add(_order(client_name="Test Client", work_order_no=f"order_{i:03d}"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="Test", db=db)

    # Should return exactly 100 (capped)
    assert len(context["results"]) == 100


def test_search_whitespace_handling():
    """Search handles leading/trailing whitespace."""
    engine = _database()
    with Session(engine, expire_on_commit=False) as db:
        operator = _operator(db)
        db.add(_order(client_name="Dent Studio", work_order_no="12345"))
        db.commit()

        context = queue_router_mod.get_search(request=_request(operator.id), q="  Dent  ", db=db)

    assert len(context["results"]) == 1
    assert context["results"][0].client_name == "Dent Studio"
