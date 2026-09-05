"""Екран «Клієнти»: список карток, права панель, прив'язка теки в `export`.

Прив'язка теки живе саме тут, на клієнті, а не на видачі: це властивість
клієнта, що діє на всі майбутні дні. Ранкова видача її просто читає — і
підтверджений ClientNameAlias назавжди прибирає «папку не знайдено» для
цього клієнта.
"""

import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.client_profile import (
    count_matching_orders,
    find_matching_orders,
    index_orders_by_name,
    summarize_client_orders,
)
from app.models import Client, ClientNameAlias, Order
from app.routers.deps import get_current_user, login_redirect, get_db, templates
from app.services.clients import (
    CLIENT_STATE_FILTERS,
    client_folder_options,
    ensure_client_profiles,
    quantity_units,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def client_pane_context(db: Session, client: Client, named_orders: list[Order] | None = None) -> dict:
    """Everything the right-hand card shows for one client. Split out so the
    shell render and the HTMX pane request build it identically.

    `named_orders` lets a caller that has already loaded them pass them in: the
    list screen loads every named order to count works per client, and rebuilding
    that set here made /clients read the whole Order table TWICE per request. The
    standalone pane route has no such list, so it loads its own."""
    if named_orders is None:
        named_orders = db.scalars(select(Order).where(Order.client_name.isnot(None))).all()
    matched = find_matching_orders(client.canonical_name, named_orders)
    summary = summarize_client_orders(matched)
    folder_names, bound_folder, folder_suggestions = client_folder_options(db, client.canonical_name)

    # Other spellings this client answers to in the sheet — shown so it is clear
    # why two names collapse into one card.
    canonical_fold = client.canonical_name.strip().casefold()
    aliases = sorted({
        (o.client_name or "").strip() for o in matched
        if (o.client_name or "").strip().casefold() != canonical_fold
    })

    return {
        "client": client,
        "summary": summary,
        "units_total": sum(quantity_units(o.quantity) for o in matched),
        "aliases": aliases,
        "folder_names": folder_names,
        "bound_folder": bound_folder,
        "folder_suggestions": folder_suggestions,
    }


@router.get("/clients/{client_id}/pane", response_class=HTMLResponse)
def get_client_pane(request: Request, client_id: int, db: Session = Depends(get_db)):
    """Right-hand card only — the list stays put, so configuring a few hundred
    clients is a sequence of small requests instead of a page load each time."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="клієнта не знайдено")

    return templates.TemplateResponse(
        request, "_client_pane.html", {"user": user, **client_pane_context(db, client)}
    )


@router.get("/clients", response_class=HTMLResponse)
def get_clients(
    request: Request,
    q: str = "",
    state: str = "all",
    selected: int | None = None,
    db: Session = Depends(get_db),
):
    """Screen: list of client profiles (CLAUDE.md — not admin-gated, any
    operator can view/manage clients, same as /stats).

    Order counts are computed via app.client_profile.find_matching_orders
    against every order that has a client_name — see that module's
    docstring for why this is a read-time fuzzy match rather than a stored
    FK, and why doing this over the full Order table on every request is
    fine at this project's scale.
    """
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    _started = time.monotonic()
    named_orders = db.scalars(select(Order).where(Order.client_name.isnot(None))).all()
    ensure_client_profiles(db, named_orders)
    clients = db.scalars(select(Client).order_by(Client.canonical_name)).all()

    # Folder bindings, so the list can show at a glance who is still unconfigured
    # — that is the state that breaks the morning handout.
    bound = {
        a.sheet_name.strip().casefold(): a.export_folder_name
        for a in db.scalars(select(ClientNameAlias).where(ClientNameAlias.confirmed.is_(True))).all()
    }

    # Один прохід по роботах замість «кожен клієнт × кожна робота»: у таблиці
    # кілька сотень різних написань імені на тисячі рядків, тож нечітке
    # порівняння повторювалось намарно тисячі разів на кожен показ екрана.
    name_index = index_orders_by_name(named_orders)
    client_rows = [
        {
            "client": client,
            "order_count": count_matching_orders(client.canonical_name, name_index),
            "bound_folder": bound.get((client.canonical_name or "").strip().casefold()),
        }
        for client in clients
    ]

    # Search and state filter run over the built rows: at this scale (a few
    # hundred) that is cheaper than a second pass over the orders, and it keeps
    # the counts in the filter chips honest — they always describe the SAME set
    # the search is narrowing.
    if state not in CLIENT_STATE_FILTERS:
        state = "all"
    state_counts = {
        "all": len(client_rows),
        "unbound": sum(1 for r in client_rows if not r["bound_folder"]),
        "active": sum(1 for r in client_rows if r["order_count"]),
    }

    needle = q.strip().casefold()
    if needle:
        client_rows = [
            r for r in client_rows
            if needle in (r["client"].canonical_name or "").casefold()
            or needle in (r["bound_folder"] or "").casefold()
            or needle in (r["client"].phone or "").casefold()
            or needle in (r["client"].email or "").casefold()
        ]
    if state == "unbound":
        client_rows = [r for r in client_rows if not r["bound_folder"]]
    elif state == "active":
        client_rows = [r for r in client_rows if r["order_count"]]

    # The pane opens on the requested client, else on the first one in view —
    # the screen is never a dead half-empty split.
    visible_ids = [r["client"].id for r in client_rows]
    selected_id = selected if selected in visible_ids else (visible_ids[0] if visible_ids else None)
    pane = (
        client_pane_context(db, db.get(Client, selected_id), named_orders=named_orders)
        if selected_id else {}
    )

    # Таймінг у лог з тієї ж причини, що й на видачі: «не відкривається» без
    # цифри — це вгадування.
    logger.info(
        "Clients screen: %d клієнтів, %d робіт з іменем, %.2fс",
        len(clients), len(named_orders), time.monotonic() - _started,
    )
    return templates.TemplateResponse(
        request,
        "clients.html",
        {
            "user": user,
            "client_rows": client_rows,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved") is not None,
            "q": q,
            "state": state,
            "state_counts": state_counts,
            "selected_id": selected_id,
            **pane,
        },
    )


@router.post("/clients", response_class=HTMLResponse)
def create_client(
    request: Request,
    canonical_name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    name = canonical_name.strip()
    if not name:
        return RedirectResponse("/clients?error=ім'я+клієнта+обов'язкове", status_code=303)

    client = Client(
        canonical_name=name,
        phone=phone.strip() or None,
        email=email.strip() or None,
        notes=notes.strip() or None,
    )
    db.add(client)
    db.commit()

    return RedirectResponse(f"/clients/{client.id}", status_code=303)


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def get_client_detail(request: Request, client_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="клієнта не знайдено")

    named_orders = db.scalars(select(Order).where(Order.client_name.isnot(None))).all()
    matched_orders = find_matching_orders(client.canonical_name, named_orders)
    summary = summarize_client_orders(matched_orders)

    # Folder binding lives HERE, on the client, not on the handout screen: it is
    # a property of the client that holds for every future day, so it is set once
    # and the morning handout just reads it. Storage stays ClientNameAlias
    # (keyed by the free-text sheet name Order.client_name carries) — Client is
    # deliberately not FK-linked to Order, see the Client model docstring.
    folder_names, bound_folder, folder_suggestions = client_folder_options(db, client.canonical_name)

    return templates.TemplateResponse(
        request,
        "client_detail.html",
        {
            "user": user,
            "client": client,
            "summary": summary,
            "saved": request.query_params.get("saved") is not None,
            "folder_names": folder_names,
            "bound_folder": bound_folder,
            "folder_suggestions": folder_suggestions,
        },
    )


@router.post("/clients/{client_id}/folder", response_class=HTMLResponse)
def bind_client_folder(
    request: Request,
    client_id: int,
    export_folder_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Bind (or unbind, with an empty value) the client's folder in `export`.

    Writes a CONFIRMED ClientNameAlias, which is exactly what the handout's
    matcher already treats as authoritative — so a binding made once here stops
    every future «папку не знайдено автоматично» for this client, and the STL
    preview has something to show."""
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="клієнта не знайдено")

    value = export_folder_name.strip()
    alias = db.scalar(
        select(ClientNameAlias).where(ClientNameAlias.sheet_name == client.canonical_name)
    )
    if not value:
        if alias is not None:
            db.delete(alias)
    elif alias is None:
        db.add(ClientNameAlias(
            sheet_name=client.canonical_name, export_folder_name=value,
            confirmed=True, confirmed_at=datetime.utcnow(),
        ))
    else:
        alias.export_folder_name = value
        alias.confirmed = True
        alias.confirmed_at = datetime.utcnow()
    db.commit()

    # From the Майстер the reply is the card itself plus an out-of-band swap of
    # that one row in the list — re-rendering the whole list would cost a fuzzy
    # match over every client (~0.8s at 280) just to flip one dot.
    if request.headers.get("HX-Request") == "true":
        # Known cost: this reads every named Order to summarise ONE client, the
        # same shape /clients/{id}/pane pays. Bounding it means narrowing the
        # candidate set before the fuzzy match, which would change matching
        # semantics — left as a deliberate trade-off rather than papered over by
        # moving the identical query up a level.
        return templates.TemplateResponse(
            request, "_client_pane.html",
            {"user": user, "swap_list_item": True, "bound_now": bool(value),
             **client_pane_context(db, client)},
        )
    return RedirectResponse(f"/clients/{client_id}?saved=1", status_code=303)


@router.post("/clients/{client_id}", response_class=HTMLResponse)
def update_client(
    request: Request,
    client_id: int,
    phone: str = Form(""),
    email: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if user is None:
        return login_redirect(request)

    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="клієнта не знайдено")

    client.phone = phone.strip() or None
    client.email = email.strip() or None
    client.notes = notes.strip() or None
    db.commit()

    return RedirectResponse(f"/clients/{client_id}?saved=1", status_code=303)
