"""Typeahead suggestions for the manual add-work form (HTMX fragments).

One endpoint per field; both return the same `_suggest_list.html` fragment so
the client controller (materialsuggest.js) is field-agnostic. Login-gated but
never redirects — an unauthenticated or empty request yields an empty list, so
the combobox simply shows nothing rather than swapping in a login page.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routers.deps import get_current_user, get_db, templates
from app.services.client_suggest import suggest_clients
from app.services.material_suggest import suggest_materials

router = APIRouter()


@router.get("/suggest/material", response_class=HTMLResponse)
def suggest_material(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    items = suggest_materials(db, q) if user is not None else []
    return templates.TemplateResponse(
        request, "_suggest_list.html", {"items": items, "field": "material"}
    )


@router.get("/suggest/client", response_class=HTMLResponse)
def suggest_client(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    items = suggest_clients(db, q) if user is not None else []
    return templates.TemplateResponse(
        request, "_suggest_list.html", {"items": items, "field": "client"}
    )
